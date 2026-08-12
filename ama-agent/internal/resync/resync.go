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
	"github.com/2kwanghee/AMX/ama-agent/internal/store"
	"github.com/2kwanghee/AMX/ama-agent/internal/tsamx"
	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// Config assembles a Resyncer.
type Config struct {
	AgentID string
	Store   *store.Store
	KEKs    *store.KEKHolder
	Bridge  tsamx.Bridge
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
	bridge      tsamx.Bridge
	engine      *sync.Mutex
	credPath    string
	fingerprint func([]byte) string
	serverCred  func() string
	send        func(*amxv1.CredentialUpdate) bool
	now         func() time.Time
	logf        func(string, ...any)
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
		engine:      engine,
		credPath:    cfg.CredentialsPath,
		fingerprint: cfg.Fingerprint,
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
	rec, ok := r.store.FindByEmail(status.ActiveEmail)
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

func wipe(b []byte) {
	for i := range b {
		b[i] = 0
	}
}
