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
	aad := []byte(amsID + "\x1f" + testAgentID)
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
		CommandId: "d1",
		IssuedAt:  timestamppb.New(time.Now().Add(-2 * DefaultAcceptanceWindow)),
		Cmd:       &amxv1.AmsCommand_ReqReport{ReqReport: &amxv1.RequestReport{}},
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

func countPrefix(calls []string, prefix string) int {
	n := 0
	for _, c := range calls {
		if len(c) >= len(prefix) && c[:len(prefix)] == prefix {
			n++
		}
	}
	return n
}
