package command

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"testing"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/crypto"
	"github.com/2kwanghee/AMX/ama-agent/internal/store"
	"github.com/2kwanghee/AMX/ama-agent/internal/tsamx"
	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	testAgentID = "ama_test"
	testKeyID   = "k1"
)

type harness struct {
	h     *Handler
	fake  *tsamx.Fake
	priv  ed25519.PrivateKey
	kek   []byte
	store *store.Store
	appl  *store.AppliedLog
}

func newHarness(t *testing.T) *harness {
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
	// Deliver the KEK via a signed SessionSetup, as AMS would each session.
	hn.apply(t, hn.sign(t, &amxv1.AmsCommand{
		CommandId: "setup-1",
		Cmd: &amxv1.AmsCommand_SessionSetup{SessionSetup: &amxv1.SessionSetup{
			Keys:        []*amxv1.SessionSetup_WrappedKey{{KeyId: testKeyID, WrappedKey: hn.kek}},
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
		st, err := store.Open(dir, testAgentID, keks)
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
	cmd := &amxv1.AmsCommand{
		CommandId:     "setup-1",
		TargetAgentId: testAgentID,
		IssuedAt:      timestamppb.New(time.Now()),
		Cmd: &amxv1.AmsCommand_SessionSetup{SessionSetup: &amxv1.SessionSetup{
			ServerCredential: "cred-xyz",
			Keys:             []*amxv1.SessionSetup_WrappedKey{{KeyId: testKeyID, WrappedKey: bytes.Repeat([]byte{0x33}, crypto.KEKSize)}},
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

func countPrefix(calls []string, prefix string) int {
	n := 0
	for _, c := range calls {
		if len(c) >= len(prefix) && c[:len(prefix)] == prefix {
			n++
		}
	}
	return n
}
