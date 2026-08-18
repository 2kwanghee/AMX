// Package resync implements the AMA side of O9 credential re-sync (SSOT §5.7).
//
// O9 is a ROTATING refresh token: when the local tsamx refreshes an OAuth
// credential, its refresh token rotates and the copy AMS holds in
// accounts.encrypted_secret goes stale, so a cross-server re-assignment would
// deliver a dead credential. This package detects that local rotation and pushes
// the refreshed set back to AMS, sealed under the manifest KEK, so AMS can
// re-encrypt and deliver the current copy.
//
// Detection is fingerprint-based: each tick computes the identity fingerprint of
// the live on-disk credential (the provider driver's credential-identity hash,
// identical to tsamx oauth.credential_fingerprint) and compares it against the
// baseline stamped on the manifest record by the last seal. A difference means a
// rotation; the record's baseline is advanced only after AMS accepts the push,
// so a drop while disconnected is retried on the next tick (best-effort, §5.7 —
// on permanent loss the re-assignment path falls back to §5.5 re-auth).
//
// Security (§7): the plaintext credential and KEK are never logged and are wiped
// the moment they are no longer needed; the fingerprint that IS retained is a
// one-way hash, not the credential.
package resync

import (
	"context"
	"errors"
	"os"
	"sync"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/crypto"
	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
	"github.com/2kwanghee/AMX/ama-agent/internal/store"
	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// Config assembles a Resyncer.
type Config struct {
	AgentID string
	Store   *store.Store
	KEKs    *store.KEKHolder
	Bridge  provider.Bridge
	// Provider is the vendor key this resyncer's Bridge serves. It scopes the
	// manifest lookup so the active account is resolved as (provider, email), not
	// email alone. Empty normalizes to "claude".
	Provider string
	// Engine is the shared tsamx serialization lock (R3). The credential file is
	// read while it is held, so a detection can never interleave with an auto tick
	// (tsamx add/switch) rewriting it. Never nil in production; a nil falls back to
	// a private mutex (tests without a scheduler).
	Engine *sync.Mutex
	// CredentialsPath is the live active-account credential file (the provider
	// driver's credential path under the config home). Empty disables re-sync.
	CredentialsPath string
	// Fingerprint is the provider driver's credential-identity hash, used to detect
	// a local rotation against the manifest baseline. Never nil.
	Fingerprint func([]byte) string
	// HasMaterial is the provider driver's HasCredentialMaterial: false only for a
	// credential set that definitely carries no token material (a logged-out
	// shell). detect() drops such a set instead of pushing it upstream. nil
	// disables the check entirely (the pre-guard behaviour), so Tick does not gate
	// on it.
	HasMaterial func([]byte) bool
	// OnUnusable reports a HasMaterial drop upward so the FIRST incident is
	// visible to an operator instead of only appearing in a log line. It is
	// EDGE-triggered: called on the tick a drop is first seen for an account, then
	// not again until the on-disk credential carries material once more or the
	// active account changes. nil disables the notification and changes nothing
	// else (same convention as HasMaterial). It runs while the engine lock is held,
	// so it must not block on the network — an Outbox enqueue (local append) is the
	// intended implementation. rec carries identifiers only; the credential
	// material is never passed (§7).
	OnUnusable func(rec store.Record)
	// ServerCredential returns the long-lived session credential presented on the
	// wire (CredentialUpdate.server_credential). Never nil.
	ServerCredential func() string
	// Send pushes one CredentialUpdate best-effort and reports whether the
	// transport accepted it (queued/sent). true advances the baseline; false leaves
	// it so the next tick retries. Never nil.
	Send func(*amxv1.CredentialUpdate) bool
	Now  func() time.Time
	Logf func(string, ...any)
}

// Resyncer detects a local refresh-token rotation and pushes the refreshed
// credential set to AMS.
type Resyncer struct {
	agentID     string
	store       *store.Store
	keks        *store.KEKHolder
	bridge      provider.Bridge
	providerKey string
	engine      *sync.Mutex
	credPath    string
	fingerprint func([]byte) string
	hasMaterial func([]byte) bool
	onUnusable  func(store.Record)
	serverCred  func() string
	send        func(*amxv1.CredentialUpdate) bool
	now         func() time.Time
	logf        func(string, ...any)

	// unusableEmail is the account (by email — the key the manifest lookup uses)
	// currently judged credential-unusable, "" when none is. It is the edge-trigger
	// state for OnUnusable: only a transition into a NEW value fires the callback,
	// so a condition that persists across ticks is reported once. Read and written
	// only inside detect, which holds the engine lock, so it needs no lock of its
	// own.
	unusableEmail string
}

// New validates cfg and returns a Resyncer.
func New(cfg Config) *Resyncer {
	engine := cfg.Engine
	if engine == nil {
		engine = &sync.Mutex{}
	}
	now := cfg.Now
	if now == nil {
		now = time.Now
	}
	logf := cfg.Logf
	if logf == nil {
		logf = func(string, ...any) {}
	}
	return &Resyncer{
		agentID:     cfg.AgentID,
		store:       cfg.Store,
		keks:        cfg.KEKs,
		bridge:      cfg.Bridge,
		providerKey: provider.Normalize(cfg.Provider),
		engine:      engine,
		credPath:    cfg.CredentialsPath,
		fingerprint: cfg.Fingerprint,
		hasMaterial: cfg.HasMaterial,
		onUnusable:  cfg.OnUnusable,
		serverCred:  cfg.ServerCredential,
		send:        cfg.Send,
		now:         now,
		logf:        logf,
	}
}

// Tick runs one detect-and-maybe-push cycle. It is safe to call on a ticker; a
// tick with nothing to do is cheap (a Status read, a file read, one hash).
func (r *Resyncer) Tick(ctx context.Context) {
	if r == nil || r.credPath == "" || r.store == nil || r.keks == nil || r.bridge == nil || r.send == nil || r.fingerprint == nil {
		return
	}
	upd, plaintext, rec, ok := r.detect(ctx)
	if !ok {
		return
	}
	// Send outside the engine lock: the transport Send may block on its buffer,
	// and holding the tsamx serialization lock across a network call would stall
	// every deliver/recall/tick. The plaintext lives only until the baseline is
	// committed, then is wiped.
	defer wipe(plaintext)
	if !r.send(upd) {
		// Disconnected / buffer full: leave the baseline stale so the next tick
		// re-detects the same rotation and retries (best-effort, §5.7).
		return
	}
	// Accepted: advance the baseline (reseal the manifest under the active KEK and
	// restamp the fingerprint) so the same rotation is not pushed again. A
	// duplicate push would be harmless anyway — AMS keeps the newest observed_at
	// (monotonic) — but this stops the steady-state resend.
	// Advance the baseline of the EXISTING record only. Upsert here would re-insert
	// a record a concurrent recall may have purged, or revive one it flipped to
	// inactive, in the lock-free window since detect() read it (R3 race). A record
	// gone (ErrNotFound) means the account was recalled while the push was in
	// flight — nothing to keep a baseline for, so skip silently.
	if err := r.store.UpdateBaseline(rec.AMSAccountID, plaintext); err != nil && !errors.Is(err, store.ErrNotFound) {
		// Baseline not advanced -> next tick retries the push; the duplicate is
		// harmless. Never include credential material in the log.
		r.logf("resync: baseline update failed for %s: %v", rec.Email, err)
	}
}

// detect reads the live credential under the engine lock, compares its
// fingerprint against the manifest baseline, and — on a change — seals the wire
// envelope. It returns ok=false (and wipes any plaintext it read) when there is
// nothing to push. The returned plaintext is owned by the caller, which MUST
// wipe it.
func (r *Resyncer) detect(ctx context.Context) (*amxv1.CredentialUpdate, []byte, store.Record, bool) {
	r.engine.Lock()
	defer r.engine.Unlock()

	// No KEK (cold start before SessionSetup): we cannot seal, and the manifest
	// baseline is unreadable. Skip safely until a session installs the KEK.
	if !r.keks.HasKeys() {
		return nil, nil, store.Record{}, false
	}
	status, err := r.bridge.Status(ctx)
	if err != nil || status == nil || status.ActiveEmail == "" {
		return nil, nil, store.Record{}, false
	}
	rec, ok := r.store.FindByProviderEmail(r.providerKey, status.ActiveEmail)
	if !ok {
		// Active account was never delivered by AMS -> nothing to re-sync.
		return nil, nil, store.Record{}, false
	}
	plaintext, err := os.ReadFile(r.credPath)
	if err != nil || len(plaintext) == 0 {
		// Missing/empty credential file: nothing to compare (do not treat as a
		// change — that would push an empty credential).
		return nil, nil, store.Record{}, false
	}
	// The length check above only catches a truncated file. A logged-out shell —
	// {"claudeAiOauth":{"accessToken":"","refreshToken":"",…}} — is non-empty bytes
	// whose fingerprint necessarily differs from the baseline, so the comparison
	// below would read it as a rotation and push the token-less set upstream, where
	// AMS would overwrite the live copy it holds. Drop it here, ahead of the
	// comparison, and leave the baseline where it is: when the real credential
	// returns, the next tick still sees it as a rotation against the OLD baseline
	// and pushes it then. nil HasMaterial disables the check.
	if r.hasMaterial != nil && !r.hasMaterial(plaintext) {
		wipe(plaintext)
		// Identifier only — never the credential material (§7).
		r.logf("resync: skipping push for %s: on-disk credential carries no token material", rec.Email)
		r.markUnusable(rec)
		return nil, nil, store.Record{}, false
	}
	// Past the guard the live credential carries material, so any earlier incident
	// is over: clear the edge-trigger state so a LATER one is reported again. Only
	// this point proves recovery — the early returns above (no KEK, no active
	// account, unreadable file) observe nothing about the material and must leave
	// the state alone.
	r.unusableEmail = ""
	if r.fingerprint(plaintext) == rec.Fingerprint {
		wipe(plaintext) // unchanged: no rotation since the last seal
		return nil, nil, store.Record{}, false
	}

	// Rotation detected: seal the refreshed set into a wire envelope. The AAD is
	// the WIRE AAD (crypto.WireAAD — the same binding AMS opens deliver with), NOT
	// the manifest's local AAD; AMS will open this envelope with WireAAD too.
	kek, keyID, ok := r.keks.ActiveKey()
	if !ok {
		wipe(plaintext)
		return nil, nil, store.Record{}, false
	}
	nonce, err := crypto.NewNonce()
	if err != nil {
		wipe(kek)
		wipe(plaintext)
		return nil, nil, store.Record{}, false
	}
	aad := crypto.WireAAD(rec.AMSAccountID, r.agentID)
	ct, sealErr := crypto.Seal(kek, nonce, plaintext, aad)
	wipe(kek)
	if sealErr != nil {
		wipe(plaintext)
		return nil, nil, store.Record{}, false
	}

	upd := &amxv1.CredentialUpdate{
		Account: &amxv1.AccountRef{
			AmsAccountId: rec.AMSAccountID,
			Email:        rec.Email,
			AccountUuid:  rec.AccountUUID,
			Provider:     r.providerKey,
		},
		EncryptedCredential: &amxv1.EncryptedCredential{
			Algorithm:       amxv1.EncryptionAlgorithm_ENCRYPTION_ALGORITHM_AES_256_GCM,
			Ciphertext:      ct,
			Nonce:           nonce,
			KeyId:           keyID,
			AadAmsAccountId: rec.AMSAccountID,
			AadAgentId:      r.agentID,
		},
		ServerCredential: r.serverCred(),
		// Wall-clock observation time: survives reboots (unlike a counter) and lets
		// AMS keep the newest per account (monotonicity, cf. P3 SetPolicy).
		ObservedAt: timestamppb.New(r.now().UTC()),
	}
	return upd, plaintext, rec, true
}

// markUnusable fires OnUnusable exactly once per incident. A tick that re-observes
// the SAME account already reported is silent (the guard drops on every tick at
// the report interval, and one event per tick would bury the signal it exists to
// raise); a different account is a different incident and fires again. Called from
// detect with the engine lock held.
func (r *Resyncer) markUnusable(rec store.Record) {
	if r.unusableEmail == rec.Email {
		return
	}
	r.unusableEmail = rec.Email
	if r.onUnusable != nil {
		r.onUnusable(rec)
	}
}

func wipe(b []byte) {
	for i := range b {
		b[i] = 0
	}
}
