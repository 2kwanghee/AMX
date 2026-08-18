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
	testAMSID2 = "acc-2"
	testEmail2 = "b@x.io"
	credV1     = `{"claudeAiOauth":{"accessToken":"a1","refreshToken":"r1"}}`
	credV2     = `{"claudeAiOauth":{"accessToken":"a2","refreshToken":"r2"}}`
	credV1Acc2 = `{"claudeAiOauth":{"accessToken":"a3","refreshToken":"r1"}}` // access rotated, refresh same
	// A logged-out shell: the file the local tooling leaves behind after a logout
	// — well-formed JSON, non-empty bytes, no token material at all.
	credEmptyTokens = `{"claudeAiOauth":{"accessToken":"","refreshToken":"","expiresAt":0,"scopes":[]}}`
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
	unusable []store.Record
	sendOK   bool
	now      time.Time
}

func newHarness(t *testing.T, kh *store.KEKHolder) *harness {
	t.Helper()
	return newHarnessWithMaterial(t, kh, claude.New().HasCredentialMaterial)
}

// newHarnessWithMaterial builds the same harness with an explicit HasMaterial
// hook, so a test can exercise the nil (check-disabled) contract.
func newHarnessWithMaterial(t *testing.T, kh *store.KEKHolder, hasMaterial func([]byte) bool) *harness {
	t.Helper()
	return newHarnessWithHooks(t, kh, hasMaterial, true)
}

// newHarnessWithHooks additionally controls whether OnUnusable is wired at all:
// wireUnusable=false leaves it nil, the contract a deployment that wants no
// notification relies on.
func newHarnessWithHooks(t *testing.T, kh *store.KEKHolder, hasMaterial func([]byte) bool, wireUnusable bool) *harness {
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
	var onUnusable func(store.Record)
	if wireUnusable {
		onUnusable = func(rec store.Record) {
			h.mu.Lock()
			defer h.mu.Unlock()
			h.unusable = append(h.unusable, rec)
		}
	}
	h.r = New(Config{
		AgentID:          testAgent,
		Store:            st,
		KEKs:             kh,
		Bridge:           h.bridge,
		Engine:           h.engine,
		CredentialsPath:  credPath,
		Fingerprint:      drv.Fingerprint,
		HasMaterial:      hasMaterial,
		OnUnusable:       onUnusable,
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

// unusableSeen returns the records OnUnusable was called with, in order.
func (h *harness) unusableSeen() []store.Record {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]store.Record(nil), h.unusable...)
}

// seedSecond installs a SECOND delivered account and makes it the active one,
// leaving the credential file where it is.
func (h *harness) seedSecond(t *testing.T, cred string) {
	t.Helper()
	if err := h.st.Upsert(store.Record{AMSAccountID: testAMSID2, Email: testEmail2}, []byte(cred)); err != nil {
		t.Fatal(err)
	}
	if err := h.bridge.Add(context.Background(), provider.AddRequest{Email: testEmail2, Enable: true}); err != nil {
		t.Fatal(err)
	}
	h.bridge.SetActiveEmail(testEmail2)
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

// TestEmptyTokenSetNotPushed: a logged-out credential shell (non-empty bytes, no
// token material) must not be pushed, and the baseline must NOT advance — so the
// real credential that comes back afterwards is still seen as a rotation and
// reaches AMS. Without the guard the shell's fingerprint alone would read as a
// rotation and overwrite the AMS copy.
func TestEmptyTokenSetNotPushed(t *testing.T) {
	h := newHarness(t, keks(t))
	h.seedDelivered(t, credV1)

	h.writeCred(t, credEmptyTokens)
	h.r.Tick(context.Background())
	if got := h.sentCount(); got != 0 {
		t.Fatalf("token-less credential pushed: %d", got)
	}

	// Recovery: real credentials return -> the rotation is detected against the
	// UNADVANCED baseline and pushed.
	h.writeCred(t, credV2)
	h.r.Tick(context.Background())
	if got := h.sentCount(); got != 1 {
		t.Fatalf("recovered credential not pushed: %d", got)
	}
	ec := h.sent[0].GetEncryptedCredential()
	pt, err := crypto.Open(kekBytes(), ec.GetNonce(), ec.GetCiphertext(), crypto.WireAAD(testAMSID, testAgent))
	if err != nil {
		t.Fatalf("wire envelope did not open: %v", err)
	}
	if string(pt) != credV2 {
		t.Fatalf("pushed plaintext is not the recovered credential: %q", pt)
	}
}

// TestNilHasMaterialSkipsCheck: HasMaterial nil is "check disabled" — the
// pre-guard behaviour, where any fingerprint change (including a token-less set)
// is pushed.
func TestNilHasMaterialSkipsCheck(t *testing.T) {
	h := newHarnessWithMaterial(t, keks(t), nil)
	h.seedDelivered(t, credV1)

	h.writeCred(t, credEmptyTokens)
	h.r.Tick(context.Background())
	if got := h.sentCount(); got != 1 {
		t.Fatalf("nil HasMaterial must not gate the push: %d", got)
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

// TestUnusableCallbackEdgeTriggered: the material guard drops on EVERY tick while
// the credential stays token-less (the guard is stateless), so OnUnusable must be
// edge-triggered — one call per incident, not one per tick — or the signal drowns
// in its own repetitions at the report cadence. Recovery must re-arm it.
func TestUnusableCallbackEdgeTriggered(t *testing.T) {
	h := newHarness(t, keks(t))
	h.seedDelivered(t, credV1)

	h.writeCred(t, credEmptyTokens)
	h.r.Tick(context.Background())
	h.r.Tick(context.Background())
	h.r.Tick(context.Background())
	if got := h.sentCount(); got != 0 {
		t.Fatalf("token-less credential pushed: %d", got)
	}
	seen := h.unusableSeen()
	if len(seen) != 1 {
		t.Fatalf("want exactly 1 notification across 3 dropping ticks, got %d", len(seen))
	}
	// Identifiers the AMS-side alert is keyed on must be present.
	if seen[0].AMSAccountID != testAMSID || seen[0].Email != testEmail {
		t.Fatalf("wrong account in notification: %+v", seen[0])
	}

	// Recovery: the credential carries material again. No new notification, and the
	// push proceeds as before the notification existed.
	h.writeCred(t, credV2)
	h.r.Tick(context.Background())
	if got := h.sentCount(); got != 1 {
		t.Fatalf("recovered credential not pushed: %d", got)
	}
	if got := len(h.unusableSeen()); got != 1 {
		t.Fatalf("recovery must not notify: %d", got)
	}

	// A SECOND incident on the same account fires again — the recovery tick above
	// cleared the edge state.
	h.writeCred(t, credEmptyTokens)
	h.r.Tick(context.Background())
	h.r.Tick(context.Background())
	if got := len(h.unusableSeen()); got != 2 {
		t.Fatalf("second incident: want 2 notifications, got %d", got)
	}
}

// TestUnusableCallbackRefiresForDifferentAccount: a different active account is a
// different incident, so it notifies even though the previous one was never seen
// to recover (the account was switched away instead).
func TestUnusableCallbackRefiresForDifferentAccount(t *testing.T) {
	h := newHarness(t, keks(t))
	h.seedDelivered(t, credV1)

	h.writeCred(t, credEmptyTokens)
	h.r.Tick(context.Background())
	if got := len(h.unusableSeen()); got != 1 {
		t.Fatalf("first account: want 1 notification, got %d", got)
	}

	// Switch to a second delivered account whose credential file is still the
	// token-less shell.
	h.seedSecond(t, credV1)
	h.r.Tick(context.Background())
	seen := h.unusableSeen()
	if len(seen) != 2 {
		t.Fatalf("second account: want 2 notifications, got %d", len(seen))
	}
	if seen[1].AMSAccountID != testAMSID2 || seen[1].Email != testEmail2 {
		t.Fatalf("second notification names the wrong account: %+v", seen[1])
	}
}

// TestNilOnUnusableKeepsGuard: OnUnusable nil is "notification disabled" — the
// guard itself must behave exactly as it did before the hook existed (drop, do not
// advance the baseline, push the recovered credential).
func TestNilOnUnusableKeepsGuard(t *testing.T) {
	h := newHarnessWithHooks(t, keks(t), claude.New().HasCredentialMaterial, false)
	h.seedDelivered(t, credV1)

	h.writeCred(t, credEmptyTokens)
	h.r.Tick(context.Background())
	if got := h.sentCount(); got != 0 {
		t.Fatalf("token-less credential pushed with nil OnUnusable: %d", got)
	}
	if got := len(h.unusableSeen()); got != 0 {
		t.Fatalf("nil OnUnusable recorded a notification: %d", got)
	}

	h.writeCred(t, credV2)
	h.r.Tick(context.Background())
	if got := h.sentCount(); got != 1 {
		t.Fatalf("recovered credential not pushed with nil OnUnusable: %d", got)
	}
}
