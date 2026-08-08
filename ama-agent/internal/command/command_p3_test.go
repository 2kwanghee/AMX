package command

import (
	"bytes"
	"context"
	"crypto/ed25519"
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
	hn.apply(t, hn.sign(t, &amxv1.AmsCommand{
		CommandId: "setup-1",
		Cmd: &amxv1.AmsCommand_SessionSetup{SessionSetup: &amxv1.SessionSetup{
			Keys:        []*amxv1.SessionSetup_WrappedKey{{KeyId: testKeyID, WrappedKey: hn.kek}},
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
