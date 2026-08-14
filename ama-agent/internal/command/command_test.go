package command

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"testing"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/crypto"
	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
	"github.com/2kwanghee/AMX/ama-agent/internal/provider/claude"
	"github.com/2kwanghee/AMX/ama-agent/internal/store"
	"github.com/2kwanghee/AMX/ama-agent/internal/tsamx"
	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
	"golang.org/x/crypto/nacl/box"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	testAgentID = "ama_test"
	testKeyID   = "k1"
)

type harness struct {
	h        *Handler
	fake     *tsamx.Fake
	priv     ed25519.PrivateKey
	kek      []byte
	agentPub *[32]byte // this session's X25519 public key (from h.NewSession)
	store    *store.Store
	appl     *store.AppliedLog
}

// sealKEK seals raw to the handler's current session public key exactly as AMS
// would: nacl.SealedBox(agent_public_key).encrypt(kek).
func (hn *harness) sealKEK(t *testing.T, raw []byte) []byte {
	t.Helper()
	sealed, err := box.SealAnonymous(nil, raw, hn.agentPub, nil)
	if err != nil {
		t.Fatal(err)
	}
	return sealed
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	keks := store.NewKEKHolder()
	st, err := store.Open(dir, testAgentID, keks, claude.New().Fingerprint)
	if err != nil {
		t.Fatal(err)
	}
	appl, err := store.OpenAppliedLog(dir)
	if err != nil {
		t.Fatal(err)
	}
	fake := tsamx.NewFake()
	h, err := New(Config{
		AgentID:   testAgentID,
		PublicKey: pub,
		Store:     st,
		KEKs:      keks,
		Applied:   appl,
		Bridge:    fake,
		Now:       time.Now,
	})
	if err != nil {
		t.Fatal(err)
	}
	hn := &harness{h: h, fake: fake, priv: priv, kek: bytes.Repeat([]byte{0x33}, crypto.KEKSize), store: st, appl: appl}
	// Establish the session key pair (as OnConnect does before Register), then
	// capture the public key so the KEK can be sealed to it as AMS would.
	pubBytes, err := h.NewSession()
	if err != nil {
		t.Fatal(err)
	}
	hn.agentPub = new([32]byte)
	copy(hn.agentPub[:], pubBytes)
	// Deliver the KEK via a signed SessionSetup, sealed to the session key.
	hn.apply(t, hn.sign(t, &amxv1.AmsCommand{
		CommandId: "setup-1",
		Cmd: &amxv1.AmsCommand_SessionSetup{SessionSetup: &amxv1.SessionSetup{
			Keys:        []*amxv1.SessionSetup_WrappedKey{{KeyId: testKeyID, WrappedKey: hn.sealKEK(t, hn.kek)}},
			ActiveKeyId: testKeyID,
		}},
	}))
	return hn
}

func (hn *harness) sign(t *testing.T, cmd *amxv1.AmsCommand) *amxv1.AmsCommand {
	t.Helper()
	if cmd.IssuedAt == nil {
		cmd.IssuedAt = timestamppb.New(time.Now())
	}
	if cmd.TargetAgentId == "" {
		cmd.TargetAgentId = testAgentID
	}
	sig, err := crypto.SignCommand(hn.priv, cmd)
	if err != nil {
		t.Fatal(err)
	}
	cmd.Signature = sig
	return cmd
}

func (hn *harness) apply(t *testing.T, cmd *amxv1.AmsCommand) *amxv1.CommandAck {
	t.Helper()
	return hn.h.Handle(context.Background(), cmd)
}

// deliverCmd builds a signed deliver whose EncryptedCredential is sealed exactly
// as AMS would: under the delivered KEK, with the LOCALLY derived AAD.
func (hn *harness) deliverCmd(t *testing.T, cmdID, amsID, email string, desired amxv1.AllocationStatus, aadAgentOverride string) *amxv1.AmsCommand {
	t.Helper()
	nonce, err := crypto.NewNonce()
	if err != nil {
		t.Fatal(err)
	}
	aad := crypto.WireAAD(amsID, testAgentID)
	plaintext := []byte(`{"accessToken":"tok-` + email + `"}`)
	ct, err := crypto.Seal(hn.kek, nonce, plaintext, aad)
	if err != nil {
		t.Fatal(err)
	}
	aadAgent := testAgentID
	if aadAgentOverride != "" {
		aadAgent = aadAgentOverride
	}
	return hn.sign(t, &amxv1.AmsCommand{
		CommandId: cmdID,
		Cmd: &amxv1.AmsCommand_Deliver{Deliver: &amxv1.DeliverAccount{
			AssignmentId:   "asg-" + amsID,
			Account:        &amxv1.AccountRef{AmsAccountId: amsID, Email: email},
			CredentialType: amxv1.CredentialType_CREDENTIAL_TYPE_OAUTH,
			DesiredStatus:  desired,
			EncryptedCredential: &amxv1.EncryptedCredential{
				Algorithm:       amxv1.EncryptionAlgorithm_ENCRYPTION_ALGORITHM_AES_256_GCM,
				Ciphertext:      ct,
				Nonce:           nonce,
				KeyId:           testKeyID,
				AadAmsAccountId: amsID,
				AadAgentId:      aadAgent,
			},
		}},
	})
}

func TestDeliverConverges(t *testing.T) {
	hn := newHarness(t)
	ack := hn.apply(t, hn.deliverCmd(t, "d1", "acc-1", "a@x.io", amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE, ""))
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_CONVERGED {
		t.Fatalf("deliver convergence = %v detail=%q", ack.Convergence, ack.Detail)
	}
	if !hn.fake.Has("a@x.io") {
		t.Fatal("account not added to pool")
	}
	if _, ok := hn.store.Get("acc-1"); !ok {
		t.Fatal("manifest record missing after deliver")
	}
}

// TestDeliverIdempotentResend: a repeat of the same command_id is a no-op that
// re-emits CONVERGED without calling the bridge Add again (SSOT §6.3 / §3).
func TestDeliverIdempotentResend(t *testing.T) {
	hn := newHarness(t)
	cmd := hn.deliverCmd(t, "d1", "acc-1", "a@x.io", amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE, "")
	hn.apply(t, cmd)
	addCalls := countPrefix(hn.fake.Calls, "add ")

	ack := hn.apply(t, cmd) // resend identical command
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_CONVERGED {
		t.Fatalf("resend convergence = %v", ack.Convergence)
	}
	if got := countPrefix(hn.fake.Calls, "add "); got != addCalls {
		t.Fatalf("bridge Add re-run on resend: %d -> %d", addCalls, got)
	}
}

// TestDeliverResendPreservesActiveNoOp (B1a idempotency): a resend of an
// already-applied deliver takes the no-op path — it must not re-run Add, and must
// not re-capture/re-restore the active account (no extra switch). The runner's
// active account is unchanged and no new bridge mutation is issued.
func TestDeliverResendPreservesActiveNoOp(t *testing.T) {
	hn := newHarness(t)
	ctx := context.Background()
	if err := hn.fake.Add(ctx, provider.AddRequest{Email: "a@x.io", Enable: true}); err != nil {
		t.Fatal(err)
	}
	cmd := hn.deliverCmd(t, "d1", "acc-b", "b@x.io", amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE, "")
	hn.apply(t, cmd) // first apply: adds B, restores active to A
	if got := hn.fake.ActiveEmail(); got != "a@x.io" {
		t.Fatalf("after first deliver active = %q, want a@x.io", got)
	}
	addCalls := countPrefix(hn.fake.Calls, "add ")
	switchCalls := countPrefix(hn.fake.Calls, "switch ")

	ack := hn.apply(t, cmd) // resend
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_CONVERGED {
		t.Fatalf("resend convergence = %v", ack.Convergence)
	}
	if got := countPrefix(hn.fake.Calls, "add "); got != addCalls {
		t.Fatalf("resend re-ran Add: %d -> %d", addCalls, got)
	}
	if got := countPrefix(hn.fake.Calls, "switch "); got != switchCalls {
		t.Fatalf("resend re-ran restore switch: %d -> %d", switchCalls, got)
	}
	if got := hn.fake.ActiveEmail(); got != "a@x.io" {
		t.Fatalf("resend moved active = %q, want a@x.io", got)
	}
}

// TestDeliverPreservesPreviousActive (B1a): delivering a NEW account must not
// move the runner's live credential. `tsamx add` activates the new slot, so
// handleDeliver restores the previously-active account — otherwise every deliver
// silently reassigns the runner to the new account and overcharges it (§6.3).
func TestDeliverPreservesPreviousActive(t *testing.T) {
	hn := newHarness(t)
	ctx := context.Background()
	// Account A is the runner's currently-active account.
	if err := hn.fake.Add(ctx, provider.AddRequest{Email: "a@x.io", Enable: true}); err != nil {
		t.Fatal(err)
	}
	if got := hn.fake.ActiveEmail(); got != "a@x.io" {
		t.Fatalf("precondition: active = %q, want a@x.io", got)
	}
	// Deliver a new account B.
	ack := hn.apply(t, hn.deliverCmd(t, "d1", "acc-b", "b@x.io", amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE, ""))
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_CONVERGED {
		t.Fatalf("deliver convergence = %v detail=%q", ack.Convergence, ack.Detail)
	}
	if !hn.fake.Has("b@x.io") {
		t.Fatal("B not added to pool")
	}
	// The runner must still be on A, not the freshly delivered B.
	if got := hn.fake.ActiveEmail(); got != "a@x.io" {
		t.Fatalf("active after deliver = %q, want a@x.io (runner must not move to the new account)", got)
	}
}

// TestDeliverFirstAccountBecomesActive (B1a): with no previously-active account
// (empty pool) there is nothing to restore to, so the first delivered account may
// stay active.
func TestDeliverFirstAccountBecomesActive(t *testing.T) {
	hn := newHarness(t)
	ack := hn.apply(t, hn.deliverCmd(t, "d1", "acc-a", "a@x.io", amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE, ""))
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_CONVERGED {
		t.Fatalf("deliver convergence = %v detail=%q", ack.Convergence, ack.Detail)
	}
	if got := hn.fake.ActiveEmail(); got != "a@x.io" {
		t.Fatalf("first delivered account should be active, got %q", got)
	}
}

// TestDeliverDesiredStatusDoesNotMoveActive (B1a): deliver adds to the pool and
// only sets enable/disable; it never changes which account is live, whether
// desired is ACTIVE (enabled rotation candidate) or INACTIVE (disabled). The
// runner stays on the previously-active account in both cases.
func TestDeliverDesiredStatusDoesNotMoveActive(t *testing.T) {
	cases := []struct {
		name         string
		desired      amxv1.AllocationStatus
		wantDisabled bool
	}{
		{"desiredActive", amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE, false},
		{"desiredInactive", amxv1.AllocationStatus_ALLOCATION_STATUS_INACTIVE, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			hn := newHarness(t)
			ctx := context.Background()
			if err := hn.fake.Add(ctx, provider.AddRequest{Email: "a@x.io", Enable: true}); err != nil {
				t.Fatal(err)
			}
			ack := hn.apply(t, hn.deliverCmd(t, "d1", "acc-b", "b@x.io", tc.desired, ""))
			if ack.Convergence != amxv1.CommandAck_CONVERGENCE_CONVERGED {
				t.Fatalf("deliver convergence = %v detail=%q", ack.Convergence, ack.Detail)
			}
			if got := hn.fake.ActiveEmail(); got != "a@x.io" {
				t.Fatalf("active = %q, want a@x.io (runner active unchanged)", got)
			}
			if d, ok := hn.fake.Disabled("b@x.io"); !ok || d != tc.wantDisabled {
				t.Fatalf("b disabled = %v (present=%v), want %v", d, ok, tc.wantDisabled)
			}
		})
	}
}

// TestRecallDisablePreservesRecord: recall with purge_local_copy=false disables
// the account and keeps the manifest record (marked INACTIVE) — O2.
func TestRecallDisablePreservesRecord(t *testing.T) {
	hn := newHarness(t)
	hn.apply(t, hn.deliverCmd(t, "d1", "acc-1", "a@x.io", amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE, ""))

	ack := hn.apply(t, hn.sign(t, &amxv1.AmsCommand{
		CommandId: "r1",
		Cmd: &amxv1.AmsCommand_Recall{Recall: &amxv1.RecallAccount{
			Account:        &amxv1.AccountRef{AmsAccountId: "acc-1", Email: "a@x.io"},
			PurgeLocalCopy: false,
		}},
	}))
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_CONVERGED {
		t.Fatalf("recall convergence = %v", ack.Convergence)
	}
	rec, ok := hn.store.Get("acc-1")
	if !ok {
		t.Fatal("recall(disable) deleted the record")
	}
	if rec.AllocationStatus != int32(amxv1.AllocationStatus_ALLOCATION_STATUS_INACTIVE) {
		t.Fatalf("record status = %d, want INACTIVE", rec.AllocationStatus)
	}
	if disabled, ok := hn.fake.Disabled("a@x.io"); !ok || !disabled {
		t.Fatal("account not disabled in pool")
	}
	if !hn.fake.Has("a@x.io") {
		t.Fatal("recall(disable) removed the account from the pool")
	}
}

func TestRecallPurgeRemovesEverything(t *testing.T) {
	hn := newHarness(t)
	hn.apply(t, hn.deliverCmd(t, "d1", "acc-1", "a@x.io", amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE, ""))
	ack := hn.apply(t, hn.sign(t, &amxv1.AmsCommand{
		CommandId: "r1",
		Cmd: &amxv1.AmsCommand_Recall{Recall: &amxv1.RecallAccount{
			Account:        &amxv1.AccountRef{AmsAccountId: "acc-1", Email: "a@x.io"},
			PurgeLocalCopy: true,
		}},
	}))
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_CONVERGED {
		t.Fatalf("purge convergence = %v", ack.Convergence)
	}
	if _, ok := hn.store.Get("acc-1"); ok {
		t.Fatal("purge kept the record")
	}
	if hn.fake.Has("a@x.io") {
		t.Fatal("purge kept the account in the pool")
	}
}

// TestRecallPurgeSwitchesAwayBeforeRemovingActive: purging the account the runner
// is live on must switch to another managed account FIRST, so Remove never leaves
// the runner reading a deleted credential (§6.3).
func TestRecallPurgeSwitchesAwayBeforeRemovingActive(t *testing.T) {
	hn := newHarness(t)
	// Two accounts; deliver leaves a@x.io active (b's deliver restores prevActive).
	hn.apply(t, hn.deliverCmd(t, "d1", "acc-1", "a@x.io", amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE, ""))
	hn.apply(t, hn.deliverCmd(t, "d2", "acc-2", "b@x.io", amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE, ""))
	if hn.fake.ActiveEmail() != "a@x.io" {
		t.Fatalf("precondition: active = %q, want a@x.io", hn.fake.ActiveEmail())
	}

	ack := hn.apply(t, hn.sign(t, &amxv1.AmsCommand{
		CommandId: "r1",
		Cmd: &amxv1.AmsCommand_Recall{Recall: &amxv1.RecallAccount{
			Account:        &amxv1.AccountRef{AmsAccountId: "acc-1", Email: "a@x.io"},
			PurgeLocalCopy: true,
		}},
	}))
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_CONVERGED {
		t.Fatalf("purge convergence = %v", ack.Convergence)
	}
	if hn.fake.Has("a@x.io") {
		t.Fatal("purge kept the recalled account in the pool")
	}
	if hn.fake.ActiveEmail() != "b@x.io" {
		t.Fatalf("runner left on %q, want switched to b@x.io before remove", hn.fake.ActiveEmail())
	}
	// The switch must precede the remove in the call log.
	calls := hn.fake.CallLog()
	switchIdx, removeIdx := -1, -1
	for i, c := range calls {
		if c == "switch b@x.io" && switchIdx == -1 {
			switchIdx = i
		}
		if c == "remove a@x.io" && removeIdx == -1 {
			removeIdx = i
		}
	}
	if switchIdx == -1 || removeIdx == -1 || switchIdx > removeIdx {
		t.Fatalf("expected switch before remove; calls=%v", calls)
	}
}

// TestRecallPurgeSingleAccountRemovesDirectly: with no other account to move to,
// purging the active account proceeds straight to Remove (the runner losing its
// only account IS the meaning of recall on a single-account host).
func TestRecallPurgeSingleAccountRemovesDirectly(t *testing.T) {
	hn := newHarness(t)
	hn.apply(t, hn.deliverCmd(t, "d1", "acc-1", "a@x.io", amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE, ""))
	ack := hn.apply(t, hn.sign(t, &amxv1.AmsCommand{
		CommandId: "r1",
		Cmd: &amxv1.AmsCommand_Recall{Recall: &amxv1.RecallAccount{
			Account:        &amxv1.AccountRef{AmsAccountId: "acc-1", Email: "a@x.io"},
			PurgeLocalCopy: true,
		}},
	}))
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_CONVERGED {
		t.Fatalf("purge convergence = %v", ack.Convergence)
	}
	if hn.fake.Has("a@x.io") {
		t.Fatal("purge kept the account in the pool")
	}
	for _, c := range hn.fake.CallLog() {
		if len(c) >= 6 && c[:6] == "switch" {
			t.Fatalf("unexpected switch on single-account purge; calls=%v", hn.fake.CallLog())
		}
	}
}

// TestRecallPurgeIdempotentWhenAlreadyAbsent: recalling an account that is no
// longer in the pool (operator removed it in tsamx directly, or a prior recall
// partially succeeded) must converge without calling `tsamx remove` — otherwise
// the not-found remove error leaves the assignment stuck in `recalling` forever.
func TestRecallPurgeIdempotentWhenAlreadyAbsent(t *testing.T) {
	hn := newHarness(t)
	// No deliver: the pool never held this account, so remove would 404.
	ack := hn.apply(t, hn.sign(t, &amxv1.AmsCommand{
		CommandId: "r1",
		Cmd: &amxv1.AmsCommand_Recall{Recall: &amxv1.RecallAccount{
			Account:        &amxv1.AccountRef{AmsAccountId: "acc-1", Email: "gone@x.io"},
			PurgeLocalCopy: true,
		}},
	}))
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_CONVERGED {
		t.Fatalf("absent purge convergence = %v (want CONVERGED)", ack.Convergence)
	}
	for _, c := range hn.fake.CallLog() {
		if len(c) >= 6 && c[:6] == "remove" {
			t.Fatalf("remove called on already-absent account; calls=%v", hn.fake.CallLog())
		}
	}
}

// TestForgedSignatureRejected: a command signed by a foreign key is REJECTED and
// has no effect.
func TestForgedSignatureRejected(t *testing.T) {
	hn := newHarness(t)
	_, foreign, _ := ed25519.GenerateKey(nil)
	cmd := &amxv1.AmsCommand{
		CommandId: "d1",
		IssuedAt:  timestamppb.New(time.Now()),
		Cmd: &amxv1.AmsCommand_Deliver{Deliver: &amxv1.DeliverAccount{
			Account: &amxv1.AccountRef{AmsAccountId: "acc-1", Email: "a@x.io"},
		}},
	}
	sig, _ := crypto.SignCommand(foreign, cmd)
	cmd.Signature = sig

	ack := hn.apply(t, cmd)
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_REJECTED {
		t.Fatalf("forged command convergence = %v, want REJECTED", ack.Convergence)
	}
	if hn.fake.Has("a@x.io") {
		t.Fatal("rejected command still had an effect")
	}
}

// TestAADAgentMismatchRejected: an EncryptedCredential whose aad_agent_id names a
// different agent is a relocated record and MUST be rejected (proto warning).
func TestAADAgentMismatchRejected(t *testing.T) {
	hn := newHarness(t)
	cmd := hn.deliverCmd(t, "d1", "acc-1", "a@x.io", amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE, "someOtherAgent")
	ack := hn.apply(t, cmd)
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_REJECTED {
		t.Fatalf("aad mismatch convergence = %v, want REJECTED", ack.Convergence)
	}
	if hn.fake.Has("a@x.io") {
		t.Fatal("rejected deliver still installed the account")
	}
}

func TestStaleCommandRejected(t *testing.T) {
	hn := newHarness(t)
	cmd := &amxv1.AmsCommand{
		CommandId:     "d1",
		TargetAgentId: testAgentID,
		IssuedAt:      timestamppb.New(time.Now().Add(-2 * DefaultAcceptanceWindow)),
		Cmd:           &amxv1.AmsCommand_ReqReport{ReqReport: &amxv1.RequestReport{}},
	}
	sig, _ := crypto.SignCommand(hn.priv, cmd)
	cmd.Signature = sig
	ack := hn.apply(t, cmd)
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_REJECTED {
		t.Fatalf("stale command convergence = %v, want REJECTED", ack.Convergence)
	}
}

func TestSetActiveTogglesStatus(t *testing.T) {
	hn := newHarness(t)
	hn.apply(t, hn.deliverCmd(t, "d1", "acc-1", "a@x.io", amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE, ""))
	ack := hn.apply(t, hn.sign(t, &amxv1.AmsCommand{
		CommandId: "sa1",
		Cmd: &amxv1.AmsCommand_SetActive{SetActive: &amxv1.SetAccountActive{
			Account: &amxv1.AccountRef{AmsAccountId: "acc-1", Email: "a@x.io"},
			Active:  false,
		}},
	}))
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_CONVERGED {
		t.Fatalf("set_active convergence = %v", ack.Convergence)
	}
	if disabled, _ := hn.fake.Disabled("a@x.io"); !disabled {
		t.Fatal("deactivate did not disable the account")
	}
}

// TestWrongRecipientRejected: a command validly signed by AMS but addressed to a
// different agent_id must be REJECTED and have no effect (recipient binding).
func TestWrongRecipientRejected(t *testing.T) {
	hn := newHarness(t)
	cmd := hn.sign(t, &amxv1.AmsCommand{
		CommandId:     "d1",
		TargetAgentId: "someOtherAgent",
		Cmd: &amxv1.AmsCommand_Deliver{Deliver: &amxv1.DeliverAccount{
			Account: &amxv1.AccountRef{AmsAccountId: "acc-1", Email: "a@x.io"},
		}},
	})
	ack := hn.apply(t, cmd)
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_REJECTED {
		t.Fatalf("wrong-recipient convergence = %v, want REJECTED", ack.Convergence)
	}
	if ack.ErrorCode != "wrong_recipient" {
		t.Fatalf("error_code = %q, want wrong_recipient", ack.ErrorCode)
	}
	if hn.fake.Has("a@x.io") {
		t.Fatal("command addressed to another agent still had an effect")
	}
}

// TestIssuedAtRequiredRejected: a command with no issued_at is REJECTED — absent
// freshness must not be a bypass (ADVERSARY).
func TestIssuedAtRequiredRejected(t *testing.T) {
	hn := newHarness(t)
	cmd := &amxv1.AmsCommand{
		CommandId:     "d1",
		TargetAgentId: testAgentID,
		Cmd:           &amxv1.AmsCommand_ReqReport{ReqReport: &amxv1.RequestReport{}},
	}
	sig, _ := crypto.SignCommand(hn.priv, cmd)
	cmd.Signature = sig
	ack := hn.apply(t, cmd)
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_REJECTED {
		t.Fatalf("missing issued_at convergence = %v, want REJECTED", ack.Convergence)
	}
}

// TestRecallReplayNoSecondEffect: replaying a recall with the same command_id
// inside the freshness window must not re-run the purge on a since-redelivered
// account (§3 replay gate).
func TestRecallReplayNoSecondEffect(t *testing.T) {
	hn := newHarness(t)
	hn.apply(t, hn.deliverCmd(t, "d1", "acc-1", "a@x.io", amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE, ""))
	recall := hn.sign(t, &amxv1.AmsCommand{
		CommandId: "r1",
		Cmd: &amxv1.AmsCommand_Recall{Recall: &amxv1.RecallAccount{
			Account:        &amxv1.AccountRef{AmsAccountId: "acc-1", Email: "a@x.io"},
			PurgeLocalCopy: true,
		}},
	})
	hn.apply(t, recall) // first purge removes the account
	if hn.fake.Has("a@x.io") {
		t.Fatal("purge did not remove the account")
	}
	// Operator re-delivers the same account under a new command_id.
	hn.apply(t, hn.deliverCmd(t, "d2", "acc-1", "a@x.io", amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE, ""))
	if !hn.fake.Has("a@x.io") {
		t.Fatal("re-deliver did not restore the account")
	}
	removesBefore := countPrefix(hn.fake.Calls, "remove ")

	ack := hn.apply(t, recall) // replay the captured recall
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_CONVERGED {
		t.Fatalf("replay convergence = %v, want CONVERGED", ack.Convergence)
	}
	if got := countPrefix(hn.fake.Calls, "remove "); got != removesBefore {
		t.Fatalf("replay re-ran the purge: remove calls %d -> %d", removesBefore, got)
	}
	if !hn.fake.Has("a@x.io") {
		t.Fatal("replay purged the re-delivered account")
	}
}

// TestServerCredentialPersistsAcrossRestart: the credential minted in
// SessionSetup is written to the sidecar and recovered by a fresh handler, so a
// restart re-authenticates over path B without the (burned) enroll_token.
func TestServerCredentialPersistsAcrossRestart(t *testing.T) {
	pub, priv, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	build := func() *Handler {
		keks := store.NewKEKHolder()
		st, err := store.Open(dir, testAgentID, keks, claude.New().Fingerprint)
		if err != nil {
			t.Fatal(err)
		}
		appl, err := store.OpenAppliedLog(dir)
		if err != nil {
			t.Fatal(err)
		}
		creds, err := store.OpenCredentialSidecar(dir)
		if err != nil {
			t.Fatal(err)
		}
		h, err := New(Config{
			AgentID: testAgentID, PublicKey: pub, Store: st, KEKs: keks,
			Applied: appl, Bridge: tsamx.NewFake(), Creds: creds, Now: time.Now,
		})
		if err != nil {
			t.Fatal(err)
		}
		return h
	}

	h1 := build()
	pubBytes, err := h1.NewSession()
	if err != nil {
		t.Fatal(err)
	}
	agentPub := new([32]byte)
	copy(agentPub[:], pubBytes)
	sealedKEK, err := box.SealAnonymous(nil, bytes.Repeat([]byte{0x33}, crypto.KEKSize), agentPub, nil)
	if err != nil {
		t.Fatal(err)
	}
	cmd := &amxv1.AmsCommand{
		CommandId:     "setup-1",
		TargetAgentId: testAgentID,
		IssuedAt:      timestamppb.New(time.Now()),
		Cmd: &amxv1.AmsCommand_SessionSetup{SessionSetup: &amxv1.SessionSetup{
			ServerCredential: "cred-xyz",
			Keys:             []*amxv1.SessionSetup_WrappedKey{{KeyId: testKeyID, WrappedKey: sealedKEK}},
			ActiveKeyId:      testKeyID,
		}},
	}
	sig, err := crypto.SignCommand(priv, cmd)
	if err != nil {
		t.Fatal(err)
	}
	cmd.Signature = sig
	if ack := h1.Handle(context.Background(), cmd); ack.Convergence != amxv1.CommandAck_CONVERGENCE_CONVERGED {
		t.Fatalf("session setup convergence = %v detail=%q", ack.Convergence, ack.Detail)
	}
	if h1.ServerCredential() != "cred-xyz" {
		t.Fatalf("in-memory credential = %q, want cred-xyz", h1.ServerCredential())
	}

	// Simulated restart: a brand-new handler over the same state dir, with no
	// SessionSetup applied, must still present the credential.
	h2 := build()
	if got := h2.ServerCredential(); got != "cred-xyz" {
		t.Fatalf("credential after restart = %q, want cred-xyz", got)
	}
}

// TestSessionSetupRejectsRawKEK: with a session established, a SessionSetup whose
// wrapped_key is a raw (unsealed) KEK is REJECTED — the C2 downgrade defense.
func TestSessionSetupRejectsRawKEK(t *testing.T) {
	hn := newHarness(t)
	ack := hn.apply(t, hn.sign(t, &amxv1.AmsCommand{
		CommandId: "setup-raw",
		Cmd: &amxv1.AmsCommand_SessionSetup{SessionSetup: &amxv1.SessionSetup{
			Keys: []*amxv1.SessionSetup_WrappedKey{{KeyId: "k2", WrappedKey: bytes.Repeat([]byte{0x44}, crypto.KEKSize)}},
		}},
	}))
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_REJECTED || ack.ErrorCode != "kek_unwrap" {
		t.Fatalf("raw KEK: convergence=%v code=%q, want REJECTED/kek_unwrap", ack.Convergence, ack.ErrorCode)
	}
}

// TestNewSessionRotatesKeyPair: a reconnect installs a fresh key pair, so a KEK
// sealed to the PRIOR session public key can no longer be unwrapped, while one
// sealed to the new key succeeds.
func TestNewSessionRotatesKeyPair(t *testing.T) {
	hn := newHarness(t)
	oldPub := new([32]byte)
	copy(oldPub[:], hn.agentPub[:])

	newPubBytes, err := hn.h.NewSession() // simulate reconnect
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Equal(oldPub[:], newPubBytes) {
		t.Fatal("reconnect did not rotate the session key")
	}

	staleSealed, err := box.SealAnonymous(nil, hn.kek, oldPub, nil)
	if err != nil {
		t.Fatal(err)
	}
	ack := hn.apply(t, hn.sign(t, &amxv1.AmsCommand{
		CommandId: "setup-stale",
		Cmd: &amxv1.AmsCommand_SessionSetup{SessionSetup: &amxv1.SessionSetup{
			Keys: []*amxv1.SessionSetup_WrappedKey{{KeyId: "k2", WrappedKey: staleSealed}},
		}},
	}))
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_REJECTED {
		t.Fatalf("KEK sealed to rotated-away key: convergence=%v, want REJECTED", ack.Convergence)
	}

	hn.agentPub = new([32]byte)
	copy(hn.agentPub[:], newPubBytes)
	ack = hn.apply(t, hn.sign(t, &amxv1.AmsCommand{
		CommandId: "setup-fresh",
		Cmd: &amxv1.AmsCommand_SessionSetup{SessionSetup: &amxv1.SessionSetup{
			Keys:        []*amxv1.SessionSetup_WrappedKey{{KeyId: "k2", WrappedKey: hn.sealKEK(t, hn.kek)}},
			ActiveKeyId: "k2",
		}},
	}))
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_CONVERGED {
		t.Fatalf("KEK sealed to fresh key: convergence=%v detail=%q, want CONVERGED", ack.Convergence, ack.Detail)
	}
}

// TestSessionSetupNoSessionKeyRejected: a SessionSetup carrying keys before any
// session key pair is established is rejected rather than silently dropping keys.
func TestSessionSetupNoSessionKeyRejected(t *testing.T) {
	pub, priv, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	keks := store.NewKEKHolder()
	st, err := store.Open(dir, testAgentID, keks, claude.New().Fingerprint)
	if err != nil {
		t.Fatal(err)
	}
	appl, err := store.OpenAppliedLog(dir)
	if err != nil {
		t.Fatal(err)
	}
	h, err := New(Config{
		AgentID: testAgentID, PublicKey: pub, Store: st, KEKs: keks,
		Applied: appl, Bridge: tsamx.NewFake(), Now: time.Now,
	})
	if err != nil {
		t.Fatal(err)
	}
	// No NewSession() call: no ephemeral key pair yet.
	cmd := &amxv1.AmsCommand{
		CommandId:     "setup-early",
		TargetAgentId: testAgentID,
		IssuedAt:      timestamppb.New(time.Now()),
		Cmd: &amxv1.AmsCommand_SessionSetup{SessionSetup: &amxv1.SessionSetup{
			Keys: []*amxv1.SessionSetup_WrappedKey{{KeyId: testKeyID, WrappedKey: bytes.Repeat([]byte{0x33}, crypto.KEKSize)}},
		}},
	}
	sig, err := crypto.SignCommand(priv, cmd)
	if err != nil {
		t.Fatal(err)
	}
	cmd.Signature = sig
	ack := h.Handle(context.Background(), cmd)
	if ack.Convergence != amxv1.CommandAck_CONVERGENCE_REJECTED || ack.ErrorCode != "no_session_key" {
		t.Fatalf("early SessionSetup: convergence=%v code=%q, want REJECTED/no_session_key", ack.Convergence, ack.ErrorCode)
	}
}

func countPrefix(calls []string, prefix string) int {
	n := 0
	for _, c := range calls {
		if len(c) >= len(prefix) && c[:len(prefix)] == prefix {
			n++
		}
	}
	return n
}

func indexOfPrefix(calls []string, prefix string) int {
	for i, c := range calls {
		if len(c) >= len(prefix) && c[:len(prefix)] == prefix {
			return i
		}
	}
	return -1
}

// TestDeliverHoldsDeliverLockAroundSwap (B1b): the deliver critical section takes
// the cross-process deliver lock BEFORE staging the credential (Add) and releases
// it only AFTER the active-account restore, so a runner cannot start up and read a
// half-swapped credential.
func TestDeliverHoldsDeliverLockAroundSwap(t *testing.T) {
	hn := newHarness(t)
	ctx := context.Background()
	if err := hn.fake.Add(ctx, provider.AddRequest{Email: "a@x.io", Enable: true}); err != nil {
		t.Fatal(err)
	}
	base := len(hn.fake.CallLog())
	hn.apply(t, hn.deliverCmd(t, "d1", "acc-b", "b@x.io", amxv1.AllocationStatus_ALLOCATION_STATUS_ACTIVE, ""))
	log := hn.fake.CallLog()[base:]

	iLock := indexOfPrefix(log, "deliver_lock")
	iAdd := indexOfPrefix(log, "add b@x.io")
	iSwitch := indexOfPrefix(log, "switch a@x.io")
	iUnlock := indexOfPrefix(log, "deliver_unlock")
	if iLock < 0 || iAdd < 0 || iSwitch < 0 || iUnlock < 0 {
		t.Fatalf("missing expected calls: %v", log)
	}
	if !(iLock < iAdd && iAdd < iSwitch && iSwitch < iUnlock) {
		t.Fatalf("deliver lock did not bracket add+restore (lock=%d add=%d switch=%d unlock=%d): %v",
			iLock, iAdd, iSwitch, iUnlock, log)
	}
}
