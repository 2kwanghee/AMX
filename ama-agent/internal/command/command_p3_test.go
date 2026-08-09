package command

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/crypto"
	"github.com/2kwanghee/AMX/ama-agent/internal/reporter"
	"github.com/2kwanghee/AMX/ama-agent/internal/scheduler"
	"github.com/2kwanghee/AMX/ama-agent/internal/store"
	"github.com/2kwanghee/AMX/ama-agent/internal/tsamx"
	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// newHarnessBridge builds a harness over a caller-supplied bridge and engine
// lock (so the same mutex can be shared with a scheduler), and delivers the KEK
// via a signed SessionSetup as AMS would.
func newHarnessBridge(t *testing.T, bridge tsamx.Bridge, engine *sync.Mutex, ob *reporter.Outbox) *harness {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	keks := store.NewKEKHolder()
	st, err := store.Open(dir, testAgentID, keks)
	if err != nil {
		t.Fatal(err)
	}
	appl, err := store.OpenAppliedLog(dir)
	if err != nil {
		t.Fatal(err)
	}
	h, err := New(Config{
		AgentID:   testAgentID,
		PublicKey: pub,
		Store:     st,
		KEKs:      keks,
		Applied:   appl,
		Bridge:    bridge,
		Engine:    engine,
		Outbox:    ob,
		Now:       time.Now,
	})
	if err != nil {
		t.Fatal(err)
	}
	hn := &harness{h: h, priv: priv, kek: bytes.Repeat([]byte{0x33}, crypto.KEKSize), store: st, appl: appl}
	pubBytes, err := h.NewSession()
	if err != nil {
		t.Fatal(err)
	}
	hn.agentPub = new([32]byte)
	copy(hn.agentPub[:], pubBytes)
	hn.apply(t, hn.sign(t, &amxv1.AmsCommand{
		CommandId: "setup-1",
		Cmd: &amxv1.AmsCommand_SessionSetup{SessionSetup: &amxv1.SessionSetup{
			Keys:        []*amxv1.SessionSetup_WrappedKey{{KeyId: testKeyID, WrappedKey: hn.sealKEK(t, hn.kek)}},
			ActiveKeyId: testKeyID,
		}},
	}))
	return hn
}

func TestSetPolicyInjectsThresholdAndStoresStrategy(t *testing.T) {
	fake := tsamx.NewFake()
	hn := newHarnessBridge(t, fake, &sync.Mutex{}, nil)

	ack := hn.apply(t, hn.sign(t, &amxv1.AmsCommand{
		CommandId: "pol-1",
		Cmd: &amxv1.AmsCommand_SetPolicy{SetPolicy: &amxv1.SetPolicy{
			ThresholdPct:    90,
			DefaultStrategy: amxv1.SwitchNow_SWITCH_STRATEGY_BEST,
		}},
	}))
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_CONVERGED {
		t.Fatalf("set_policy convergence = %v detail=%q", ack.Convergence, ack.Detail)
	}
	if fake.Threshold != 90 {
		t.Fatalf("threshold injected = %v, want 90", fake.Threshold)
	}
	if !containsPrefix(fake.CallLog(), "config set autoswitch.threshold") {
		t.Fatalf("config set not called: %v", fake.CallLog())
	}
	if hn.h.DefaultStrategy() != amxv1.SwitchNow_SWITCH_STRATEGY_BEST {
		t.Fatalf("default strategy = %v, want BEST", hn.h.DefaultStrategy())
	}
}

func TestSetPolicyZeroThresholdSkipsInjection(t *testing.T) {
	fake := tsamx.NewFake()
	hn := newHarnessBridge(t, fake, &sync.Mutex{}, nil)
	hn.apply(t, hn.sign(t, &amxv1.AmsCommand{
		CommandId: "pol-2",
		Cmd: &amxv1.AmsCommand_SetPolicy{SetPolicy: &amxv1.SetPolicy{
			ThresholdPct:    0, // keep local default -> no injection
			DefaultStrategy: amxv1.SwitchNow_SWITCH_STRATEGY_NEXT_AVAILABLE,
		}},
	}))
	if containsPrefix(fake.CallLog(), "config set autoswitch.threshold") {
		t.Fatalf("threshold injected despite pct=0: %v", fake.CallLog())
	}
	if hn.h.DefaultStrategy() != amxv1.SwitchNow_SWITCH_STRATEGY_NEXT_AVAILABLE {
		t.Fatalf("default strategy not stored")
	}
}

func TestSwitchNowUsesDefaultStrategy(t *testing.T) {
	fake := tsamx.NewFake()
	ctx := context.Background()
	_ = fake.Add(ctx, tsamx.AddRequest{Email: "a@x.io", Enable: true})
	_ = fake.Add(ctx, tsamx.AddRequest{Email: "b@x.io", Enable: true})
	fake.SetActiveEmail("a@x.io")

	ob := reporter.NewOutbox()
	hn := newHarnessBridge(t, fake, &sync.Mutex{}, ob)
	// Deliver a policy default of BEST.
	hn.apply(t, hn.sign(t, &amxv1.AmsCommand{
		CommandId: "pol-3",
		Cmd: &amxv1.AmsCommand_SetPolicy{SetPolicy: &amxv1.SetPolicy{
			DefaultStrategy: amxv1.SwitchNow_SWITCH_STRATEGY_BEST,
		}},
	}))
	// switch_now with no explicit target/strategy -> falls back to BEST.
	ack := hn.apply(t, hn.sign(t, &amxv1.AmsCommand{
		CommandId: "sw-1",
		Cmd:       &amxv1.AmsCommand_SwitchNow{SwitchNow: &amxv1.SwitchNow{}},
	}))
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_CONVERGED {
		t.Fatalf("switch_now convergence = %v detail=%q", ack.Convergence, ack.Detail)
	}
	if !containsPrefix(fake.CallLog(), "switch --strategy best") {
		t.Fatalf("expected switch --strategy best, got %v", fake.CallLog())
	}
	// A manual switch event was queued (trigger=manual).
	var got []*amxv1.AccountEvent
	_ = ob.Flush(func(ev *amxv1.AccountEvent) error { got = append(got, ev); return nil })
	if len(got) != 1 || got[0].GetTrigger() != amxv1.AccountEvent_TRIGGER_MANUAL {
		t.Fatalf("manual switch event = %+v", got)
	}
}

// TestSetPolicyRejectsStaleReplay: after the operator lowers the threshold, a
// captured older SetPolicy resent inside the freshness window must NOT rewind the
// live threshold to its past value (ADVERSARY R3 monotonicity). An equal-or-newer
// re-assertion of the latest policy still applies.
func TestSetPolicyRejectsStaleReplay(t *testing.T) {
	fake := tsamx.NewFake()
	hn := newHarnessBridge(t, fake, &sync.Mutex{}, nil)
	now := time.Now()

	// Operator sets threshold 50 (recent).
	hn.apply(t, hn.sign(t, &amxv1.AmsCommand{
		CommandId: "pol-new",
		IssuedAt:  timestamppb.New(now.Add(-30 * time.Second)),
		Cmd:       &amxv1.AmsCommand_SetPolicy{SetPolicy: &amxv1.SetPolicy{ThresholdPct: 50}},
	}))
	if fake.Threshold != 50 {
		t.Fatalf("threshold after set = %v, want 50", fake.Threshold)
	}

	// Replay a captured older SetPolicy(90) still inside the 5-minute window.
	ack := hn.apply(t, hn.sign(t, &amxv1.AmsCommand{
		CommandId: "pol-old-replay",
		IssuedAt:  timestamppb.New(now.Add(-2 * time.Minute)),
		Cmd:       &amxv1.AmsCommand_SetPolicy{SetPolicy: &amxv1.SetPolicy{ThresholdPct: 90}},
	}))
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_CONVERGED {
		t.Fatalf("stale replay convergence = %v detail=%q", ack.Convergence, ack.Detail)
	}
	if fake.Threshold != 50 {
		t.Fatalf("threshold rewound by stale replay = %v, want 50", fake.Threshold)
	}

	// A genuinely newer re-assertion still applies.
	hn.apply(t, hn.sign(t, &amxv1.AmsCommand{
		CommandId: "pol-newer",
		IssuedAt:  timestamppb.New(now),
		Cmd:       &amxv1.AmsCommand_SetPolicy{SetPolicy: &amxv1.SetPolicy{ThresholdPct: 70}},
	}))
	if fake.Threshold != 70 {
		t.Fatalf("newer re-assertion not applied, threshold = %v, want 70", fake.Threshold)
	}
}

// TestSetPolicyReassertionSameIssuedAtApplies: the normal session re-assertion
// resends the latest policy with an unchanged issued_at; monotonicity must treat
// equal issued_at as applicable, not a stale rewind.
func TestSetPolicyReassertionSameIssuedAtApplies(t *testing.T) {
	fake := tsamx.NewFake()
	hn := newHarnessBridge(t, fake, &sync.Mutex{}, nil)
	issued := time.Now().Add(-time.Minute)

	hn.apply(t, hn.sign(t, &amxv1.AmsCommand{
		CommandId: "pol-a",
		IssuedAt:  timestamppb.New(issued),
		Cmd:       &amxv1.AmsCommand_SetPolicy{SetPolicy: &amxv1.SetPolicy{ThresholdPct: 80}},
	}))
	// Re-assert the SAME policy (same issued_at); it must still converge and apply.
	ack := hn.apply(t, hn.sign(t, &amxv1.AmsCommand{
		CommandId: "pol-a-reassert",
		IssuedAt:  timestamppb.New(issued),
		Cmd:       &amxv1.AmsCommand_SetPolicy{SetPolicy: &amxv1.SetPolicy{ThresholdPct: 80}},
	}))
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_CONVERGED {
		t.Fatalf("re-assertion convergence = %v detail=%q", ack.Convergence, ack.Detail)
	}
	if fake.Threshold != 80 {
		t.Fatalf("threshold after re-assertion = %v, want 80", fake.Threshold)
	}
}

// blockingBridge blocks inside Add until released, so a test can hold the engine
// lock while a scheduler tick tries to acquire it.
type blockingBridge struct {
	*tsamx.Fake
	addStarted chan struct{}
	release    chan struct{}
}

func (b *blockingBridge) Add(ctx context.Context, req tsamx.AddRequest) error {
	close(b.addStarted)
	<-b.release
	return b.Fake.Add(ctx, req)
}

func TestEngineLockSerializesDeliverAndTick(t *testing.T) {
	engine := &sync.Mutex{}
	bb := &blockingBridge{
		Fake:       tsamx.NewFake(),
		addStarted: make(chan struct{}),
		release:    make(chan struct{}),
	}
	ob := reporter.NewOutbox()
	hn := newHarnessBridge(t, bb, engine, ob)

	sched := scheduler.New(scheduler.Config{
		AgentID:  testAgentID,
		Bridge:   bb,
		Reporter: reporter.New(testAgentID, bb, time.Now),
		Outbox:   reporter.NewOutbox(),
		Engine:   engine,
		Interval: time.Hour,
	})

	// Start a deliver; it grabs the engine lock and blocks inside Add.
	deliverDone := make(chan *amxv1.CommandAck, 1)
	go func() {
		deliverDone <- hn.apply(t, hn.deliverCmd(t, "d1", "acc-1", "a@x.io", amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE, ""))
	}()
	<-bb.addStarted // deliver now holds the engine lock

	// A tick must block on the same lock and not reach auto --once.
	tickDone := make(chan struct{})
	go func() { sched.Tick(context.Background()); close(tickDone) }()

	select {
	case <-tickDone:
		t.Fatal("tick completed while deliver held the engine lock")
	case <-time.After(50 * time.Millisecond):
	}
	if containsPrefix(bb.CallLog(), "auto --once") {
		t.Fatal("tick ran auto --once while the engine lock was held by deliver")
	}

	// Release deliver; the tick must then proceed and run auto --once.
	close(bb.release)
	<-deliverDone
	select {
	case <-tickDone:
	case <-time.After(2 * time.Second):
		t.Fatal("tick did not complete after the engine lock was released")
	}
	if !containsPrefix(bb.CallLog(), "auto --once") {
		t.Fatal("tick never ran auto --once after acquiring the lock")
	}
}

func containsPrefix(log []string, prefix string) bool {
	for _, c := range log {
		if strings.HasPrefix(c, prefix) {
			return true
		}
	}
	return false
}

// restoreFailBridge fails only the restore Switch, so a test can exercise the
// B1a "복귀 실패 -> diverged" path (add succeeds, the runner is left on the new
// account = overcharge risk that must be surfaced to AMS).
type restoreFailBridge struct {
	*tsamx.Fake
}

func (b *restoreFailBridge) Switch(_ context.Context, target string) error {
	return errors.New("restore switch failed for " + target)
}

// TestDeliverRestoreFailureDiverges (B1a): when restoring the previously-active
// account fails, deliver reports DIVERGED with error_code tsamx_restore_active so
// AMS is alerted to the overcharge window (the new account was left active).
func TestDeliverRestoreFailureDiverges(t *testing.T) {
	fake := tsamx.NewFake()
	ctx := context.Background()
	if err := fake.Add(ctx, tsamx.AddRequest{Email: "a@x.io", Enable: true}); err != nil {
		t.Fatal(err)
	}
	hn := newHarnessBridge(t, &restoreFailBridge{Fake: fake}, &sync.Mutex{}, nil)
	ack := hn.apply(t, hn.deliverCmd(t, "d1", "acc-b", "b@x.io", amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE, ""))
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_DIVERGED {
		t.Fatalf("restore-failure convergence = %v, want DIVERGED", ack.Convergence)
	}
	if ack.ErrorCode != "tsamx_restore_active" {
		t.Fatalf("error_code = %q, want tsamx_restore_active", ack.ErrorCode)
	}
	if !fake.Has("b@x.io") {
		t.Fatal("B should have been added before the failed restore")
	}
}

// statusErrBridge fails Status, so a test can exercise B1b review item 4: the
// runner's prior active account is unknown, so after Add the new account may be
// left live and deliver must surface DIVERGED (not a false CONVERGED).
type statusErrBridge struct {
	*tsamx.Fake
}

func (b *statusErrBridge) Status(_ context.Context) (*tsamx.StatusResult, error) {
	return nil, errors.New("status unavailable")
}

// TestDeliverStatusErrorDiverges (B1b item 4): a Status read failure before Add
// means the prior active account is unknown, so deliver reports DIVERGED
// (active_unknown) rather than hiding a possible over-charge behind CONVERGED. The
// account is still installed so a later reconcile/redeliver can settle it.
func TestDeliverStatusErrorDiverges(t *testing.T) {
	fake := tsamx.NewFake()
	ctx := context.Background()
	if err := fake.Add(ctx, tsamx.AddRequest{Email: "a@x.io", Enable: true}); err != nil {
		t.Fatal(err)
	}
	hn := newHarnessBridge(t, &statusErrBridge{Fake: fake}, &sync.Mutex{}, nil)
	ack := hn.apply(t, hn.deliverCmd(t, "d1", "acc-b", "b@x.io", amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE, ""))
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_DIVERGED {
		t.Fatalf("status-error convergence = %v, want DIVERGED", ack.Convergence)
	}
	if ack.ErrorCode != "active_unknown" {
		t.Fatalf("error_code = %q, want active_unknown", ack.ErrorCode)
	}
	if !fake.Has("b@x.io") {
		t.Fatal("B should still be installed for a later reconcile")
	}
}

// blockingLockBridge blocks inside DeliverLock until released, so a test can prove
// the deliver lock is taken OUTSIDE the engine lock: while a deliver waits on the
// lock, another engine-locked command must still make progress (B1b review item
// 1 — the engine can never freeze behind a runner holding the shared lock).
type blockingLockBridge struct {
	*tsamx.Fake
	lockStarted chan struct{}
	lockRelease chan struct{}
	once        sync.Once
}

func (b *blockingLockBridge) DeliverLock(ctx context.Context) func() error {
	b.once.Do(func() { close(b.lockStarted) })
	<-b.lockRelease
	return b.Fake.DeliverLock(ctx)
}

// TestDeliverLockTakenOutsideEngineLock (B1b item 1, regression): with the lock
// acquired BEFORE the engine lock, a deliver stuck waiting on the deliver lock
// does NOT hold the engine lock, so a concurrent set_active (which needs the
// engine lock) completes. On the pre-fix code (lock inside the engine lock) this
// would deadlock the engine and time out.
func TestDeliverLockTakenOutsideEngineLock(t *testing.T) {
	engine := &sync.Mutex{}
	bb := &blockingLockBridge{
		Fake:        tsamx.NewFake(),
		lockStarted: make(chan struct{}),
		lockRelease: make(chan struct{}),
	}
	ctx := context.Background()
	if err := bb.Fake.Add(ctx, tsamx.AddRequest{Email: "a@x.io", Enable: true}); err != nil {
		t.Fatal(err)
	}
	hn := newHarnessBridge(t, bb, engine, reporter.NewOutbox())

	deliverDone := make(chan struct{})
	go func() {
		hn.apply(t, hn.deliverCmd(t, "d1", "acc-b", "b@x.io", amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE, ""))
		close(deliverDone)
	}()
	<-bb.lockStarted // deliver is now parked in DeliverLock, holding NO engine lock

	// A command that needs the engine lock must still run — the engine is free.
	saDone := make(chan *amxv1.CommandAck, 1)
	go func() {
		saDone <- hn.apply(t, hn.sign(t, &amxv1.AmsCommand{
			CommandId: "sa-1",
			Cmd: &amxv1.AmsCommand_SetActive{SetActive: &amxv1.SetAccountActive{
				Account: &amxv1.AccountRef{AmsAccountId: "acc-a", Email: "a@x.io"},
				Active:  true,
			}},
		}))
	}()
	select {
	case ack := <-saDone:
		if ack.Convergence != amxv1.CommandAck_CONVERGENCE_CONVERGED {
			t.Fatalf("set_active during blocked deliver: %v detail=%q", ack.Convergence, ack.Detail)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("engine frozen: set_active blocked while deliver waited on the deliver lock")
	}

	close(bb.lockRelease) // let the deliver acquire the lock and finish
	select {
	case <-deliverDone:
	case <-time.After(2 * time.Second):
		t.Fatal("deliver did not finish after the deliver lock was released")
	}
}
