package resync

import (
	"bytes"
	"context"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/crypto"
	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
	"github.com/2kwanghee/AMX/ama-agent/internal/provider/claude"
	"github.com/2kwanghee/AMX/ama-agent/internal/store"
	"github.com/2kwanghee/AMX/ama-agent/internal/tsamx"
	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
)

const (
	testKeyID  = "k1"
	testAgent  = "ama_test"
	testAMSID  = "acc-1"
	testEmail  = "a@x.io"
	credV1     = `{"claudeAiOauth":{"accessToken":"a1","refreshToken":"r1"}}`
	credV2     = `{"claudeAiOauth":{"accessToken":"a2","refreshToken":"r2"}}`
	credV1Acc2 = `{"claudeAiOauth":{"accessToken":"a3","refreshToken":"r1"}}` // access rotated, refresh same
)

func kekBytes() []byte { return bytes.Repeat([]byte{0x22}, crypto.KEKSize) }

func keks(t *testing.T) *store.KEKHolder {
	t.Helper()
	h := store.NewKEKHolder()
	h.Put(testKeyID, kekBytes())
	if !h.SetActive(testKeyID) {
		t.Fatal("SetActive")
	}
	return h
}

// harness builds a Resyncer over a real store + a Fake bridge + a temp credential
// file, capturing every pushed CredentialUpdate. sendOK controls the transport
// accept signal.
type harness struct {
	r        *Resyncer
	st       *store.Store
	bridge   *tsamx.Fake
	credPath string
	engine   *sync.Mutex
	mu       sync.Mutex
	sent     []*amxv1.CredentialUpdate
	sendOK   bool
	now      time.Time
}

func newHarness(t *testing.T, kh *store.KEKHolder) *harness {
	t.Helper()
	dir := t.TempDir()
	drv := claude.New()
	st, err := store.Open(dir, testAgent, kh, drv.Fingerprint)
	if err != nil {
		t.Fatal(err)
	}
	credPath := drv.CredentialPath(dir)
	h := &harness{
		st:       st,
		bridge:   tsamx.NewFake(),
		credPath: credPath,
		engine:   &sync.Mutex{},
		sendOK:   true,
		now:      time.Unix(1_700_000_000, 0).UTC(),
	}
	h.r = New(Config{
		AgentID:          testAgent,
		Store:            st,
		KEKs:             kh,
		Bridge:           h.bridge,
		Engine:           h.engine,
		CredentialsPath:  credPath,
		Fingerprint:      drv.Fingerprint,
		ServerCredential: func() string { return "srv-cred" },
		Send: func(u *amxv1.CredentialUpdate) bool {
			h.mu.Lock()
			defer h.mu.Unlock()
			if h.sendOK {
				h.sent = append(h.sent, u)
			}
			return h.sendOK
		},
		Now:  func() time.Time { return h.now },
		Logf: func(string, ...any) {},
	})
	return h
}

func (h *harness) writeCred(t *testing.T, s string) {
	t.Helper()
	if err := os.WriteFile(h.credPath, []byte(s), 0o600); err != nil {
		t.Fatal(err)
	}
}

// seedDelivered installs the account into the pool (active) and seals cred into
// the manifest — the baseline a prior Deliver would leave.
func (h *harness) seedDelivered(t *testing.T, cred string) {
	t.Helper()
	if err := h.st.Upsert(store.Record{AMSAccountID: testAMSID, Email: testEmail}, []byte(cred)); err != nil {
		t.Fatal(err)
	}
	if err := h.bridge.Add(context.Background(), provider.AddRequest{Email: testEmail, Enable: true}); err != nil {
		t.Fatal(err)
	}
	h.bridge.SetActiveEmail(testEmail)
	h.writeCred(t, cred)
}

func (h *harness) sentCount() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.sent)
}

// TestRotationDetectedAndSealed: a refresh-token change produces one
// CredentialUpdate whose envelope decrypts under the KEK + wire AAD, and the
// baseline advances so a second tick is silent.
func TestRotationDetectedAndSealed(t *testing.T) {
	kh := keks(t)
	h := newHarness(t, kh)
	h.seedDelivered(t, credV1)

	// Local refresh rotates the token on disk.
	h.writeCred(t, credV2)
	h.r.Tick(context.Background())

	if got := h.sentCount(); got != 1 {
		t.Fatalf("want 1 push, got %d", got)
	}
	upd := h.sent[0]
	if upd.GetAccount().GetAmsAccountId() != testAMSID || upd.GetAccount().GetEmail() != testEmail {
		t.Fatalf("wrong account ref: %v", upd.GetAccount())
	}
	if upd.GetServerCredential() != "srv-cred" {
		t.Fatalf("server_credential not set: %q", upd.GetServerCredential())
	}
	if upd.GetObservedAt().AsTime() != h.now {
		t.Fatalf("observed_at mismatch: %v", upd.GetObservedAt().AsTime())
	}
	// Envelope decrypts to the refreshed plaintext under KEK + WIRE AAD.
	ec := upd.GetEncryptedCredential()
	if ec.GetKeyId() != testKeyID {
		t.Fatalf("key_id %q", ec.GetKeyId())
	}
	if ec.GetAadAmsAccountId() != testAMSID || ec.GetAadAgentId() != testAgent {
		t.Fatalf("aad compare fields wrong: %v", ec)
	}
	aad := crypto.WireAAD(testAMSID, testAgent)
	pt, err := crypto.Open(kekBytes(), ec.GetNonce(), ec.GetCiphertext(), aad)
	if err != nil {
		t.Fatalf("wire envelope did not open under KEK+wire AAD: %v", err)
	}
	if string(pt) != credV2 {
		t.Fatalf("decrypted plaintext mismatch: %q", pt)
	}
	// Wrong AAD must fail (binding is real, not decorative).
	if _, err := crypto.Open(kekBytes(), ec.GetNonce(), ec.GetCiphertext(), crypto.WireAAD("other", testAgent)); err == nil {
		t.Fatal("envelope opened under wrong AAD")
	}

	// Baseline advanced: a second tick with no further change pushes nothing.
	h.r.Tick(context.Background())
	if got := h.sentCount(); got != 1 {
		t.Fatalf("baseline not advanced: %d pushes after second tick", got)
	}
}

// TestNoChangeNoPush: identical bytes, and an access-token-only rotation (same
// refresh token), are both no-ops — fingerprint is refresh-token based.
func TestNoChangeNoPush(t *testing.T) {
	h := newHarness(t, keks(t))
	h.seedDelivered(t, credV1)

	h.r.Tick(context.Background()) // identical to baseline
	if got := h.sentCount(); got != 0 {
		t.Fatalf("identical credential pushed: %d", got)
	}

	// Access token rotates but refresh token is unchanged -> same lineage -> silent.
	h.writeCred(t, credV1Acc2)
	h.r.Tick(context.Background())
	if got := h.sentCount(); got != 0 {
		t.Fatalf("access-only rotation pushed: %d", got)
	}
}

// TestSendFailureRetries: when the transport rejects the push, the baseline is
// NOT advanced, so the next tick re-detects and retries.
func TestSendFailureRetries(t *testing.T) {
	h := newHarness(t, keks(t))
	h.seedDelivered(t, credV1)
	h.writeCred(t, credV2)

	h.mu.Lock()
	h.sendOK = false
	h.mu.Unlock()
	h.r.Tick(context.Background()) // rejected; nothing captured
	if got := h.sentCount(); got != 0 {
		t.Fatalf("rejected send captured: %d", got)
	}

	// Reconnect: the next tick retries the same rotation.
	h.mu.Lock()
	h.sendOK = true
	h.mu.Unlock()
	h.r.Tick(context.Background())
	if got := h.sentCount(); got != 1 {
		t.Fatalf("retry did not push after reconnect: %d", got)
	}
}

// TestNoKEKSkips: before SessionSetup delivers a KEK, a rotation is skipped
// safely (no panic, no push) — the record cannot be sealed.
func TestNoKEKSkips(t *testing.T) {
	h := newHarness(t, store.NewKEKHolder()) // empty holder
	// Cannot Upsert without a KEK; seed the pool + disk only, no manifest record.
	if err := h.bridge.Add(context.Background(), provider.AddRequest{Email: testEmail, Enable: true}); err != nil {
		t.Fatal(err)
	}
	h.bridge.SetActiveEmail(testEmail)
	h.writeCred(t, credV2)

	h.r.Tick(context.Background())
	if got := h.sentCount(); got != 0 {
		t.Fatalf("pushed without a KEK: %d", got)
	}
}

// TestUndeliveredActiveSkips: the active account has no manifest record (never
// delivered by AMS), so there is nothing to re-sync.
func TestUndeliveredActiveSkips(t *testing.T) {
	h := newHarness(t, keks(t))
	if err := h.bridge.Add(context.Background(), provider.AddRequest{Email: "stranger@x.io", Enable: true}); err != nil {
		t.Fatal(err)
	}
	h.bridge.SetActiveEmail("stranger@x.io")
	h.writeCred(t, credV2)

	h.r.Tick(context.Background())
	if got := h.sentCount(); got != 0 {
		t.Fatalf("pushed for an undelivered account: %d", got)
	}
}

// TestMissingCredentialFileSkips: no on-disk credential (nothing to compare) is a
// no-op, never a push of empty bytes.
func TestMissingCredentialFileSkips(t *testing.T) {
	h := newHarness(t, keks(t))
	if err := h.st.Upsert(store.Record{AMSAccountID: testAMSID, Email: testEmail}, []byte(credV1)); err != nil {
		t.Fatal(err)
	}
	if err := h.bridge.Add(context.Background(), provider.AddRequest{Email: testEmail, Enable: true}); err != nil {
		t.Fatal(err)
	}
	h.bridge.SetActiveEmail(testEmail)
	// deliberately do NOT write the credential file

	h.r.Tick(context.Background())
	if got := h.sentCount(); got != 0 {
		t.Fatalf("pushed with no credential file: %d", got)
	}
}

// TestTickUsesEngineLock: Tick acquires and releases the shared engine lock (the
// credential read is serialized against tsamx mutations). If it deadlocked or
// left the lock held, this would hang or fail.
func TestTickUsesEngineLock(t *testing.T) {
	h := newHarness(t, keks(t))
	h.seedDelivered(t, credV1)
	h.writeCred(t, credV2)

	h.r.Tick(context.Background())
	// Lock must be free after the tick (send happens outside it).
	if !h.engine.TryLock() {
		t.Fatal("engine lock still held after Tick")
	}
	h.engine.Unlock()
}
