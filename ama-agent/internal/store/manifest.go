// Package store implements the AMA encrypted account manifest and the plaintext
// applied-command sidecar (design note §4, SSOT §6.2).
//
//   - manifest.enc: one file, an array of records. Metadata is stored in the
//     clear for reconciliation; the credential SET of each record is sealed
//     individually with AES-256-GCM (per-record nonce). The AAD is derived
//     LOCALLY as (amsAccountId ‖ agentId) — the proto aad_* fields are for
//     comparison only and are never fed into the AEAD.
//   - applied.log: plaintext JSON-lines ring (128), survives reboot without the
//     KEK, feeds idempotency and Register.applied_command_ids.
package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/crypto"
)

// aadSeparator joins amsAccountId and agentId into the AAD. It is a control
// character that cannot appear in either field, so the binding is unambiguous.
const aadSeparator = "\x1f"

// ManifestFileName is the encrypted manifest's basename.
const ManifestFileName = "manifest.enc"

var (
	// ErrNoKEK means no key-encryption key is available (cold start before
	// SessionSetup, or the record's key was revoked).
	ErrNoKEK = errors.New("store: no KEK available (awaiting SessionSetup)")
	// ErrNotFound means the manifest has no record for the account.
	ErrNotFound = errors.New("store: account not found in manifest")
)

// Record is one account's manifest entry. Metadata is plaintext; the credential
// set lives only in Ciphertext (sealed). []byte fields marshal to base64.
type Record struct {
	AMSAccountID     string    `json:"amsAccountId"`
	Email            string    `json:"email"`
	AccountUUID      string    `json:"accountUuid,omitempty"`
	AllocationStatus int32     `json:"allocationStatus"` // amxv1.AllocationStatus
	OrganizationName string    `json:"organizationName,omitempty"`
	Algorithm        int32     `json:"algorithm"` // amxv1.EncryptionAlgorithm
	KeyID            string    `json:"keyId"`
	Nonce            []byte    `json:"nonce"`
	Ciphertext       []byte    `json:"ciphertext"` // sealed credential-set JSON — never logged
	ReceivedAt       time.Time `json:"receivedAt"`
	// Fingerprint is the stable identity hash of the sealed credential set (the
	// refresh-token hash when present, else a content hash — CredentialFingerprint,
	// mirroring tsamx oauth.credential_fingerprint). It is a one-way hash, NOT the
	// credential, so it lives in the plaintext metadata: the O9 credential re-sync
	// (§5.7) compares the live on-disk fingerprint against this baseline to detect
	// a local refresh-token rotation without decrypting the record every tick.
	Fingerprint string `json:"fingerprint,omitempty"`
}

// Store is the in-memory + on-disk manifest, guarded by a mutex.
type Store struct {
	mu      sync.Mutex
	agentID string
	path    string
	keks    *KEKHolder
	records map[string]*Record // keyed by amsAccountId
}

type manifestFile struct {
	SchemaVersion int       `json:"schemaVersion"`
	Records       []*Record `json:"records"`
}

// Open loads (or initializes) the manifest at dir/manifest.enc. keks supplies
// the in-memory KEKs; agentID binds the AAD.
func Open(dir, agentID string, keks *KEKHolder) (*Store, error) {
	if agentID == "" {
		return nil, errors.New("store: empty agentID")
	}
	if keks == nil {
		return nil, errors.New("store: nil KEK holder")
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	s := &Store{
		agentID: agentID,
		path:    filepath.Join(dir, ManifestFileName),
		keks:    keks,
		records: make(map[string]*Record),
	}
	if err := s.load(); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *Store) load() error {
	b, err := os.ReadFile(s.path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	if len(b) == 0 {
		return nil
	}
	var mf manifestFile
	if err := json.Unmarshal(b, &mf); err != nil {
		return fmt.Errorf("store: parse manifest: %w", err)
	}
	for _, r := range mf.Records {
		s.records[r.AMSAccountID] = r
	}
	return nil
}

func (s *Store) persist() error {
	mf := manifestFile{SchemaVersion: 1, Records: make([]*Record, 0, len(s.records))}
	for _, r := range s.records {
		mf.Records = append(mf.Records, r)
	}
	sort.Slice(mf.Records, func(i, j int) bool {
		return mf.Records[i].AMSAccountID < mf.Records[j].AMSAccountID
	})
	b, err := json.MarshalIndent(mf, "", "  ")
	if err != nil {
		return err
	}
	return atomicWrite(s.path, b, 0o600)
}

// aad derives the AAD locally from amsAccountId and this agent's id. The proto
// aad_* fields are NEVER used here (SSOT §6.2 warning).
func (s *Store) aad(amsAccountID string) []byte {
	return []byte(amsAccountID + aadSeparator + s.agentID)
}

// Upsert seals plaintextCred under the active KEK and writes the record. The
// caller MUST wipe plaintextCred afterwards.
func (s *Store) Upsert(rec Record, plaintextCred []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	kek, keyID, ok := s.keks.ActiveKey()
	if !ok {
		return ErrNoKEK
	}
	defer wipe(kek)
	nonce, err := crypto.NewNonce()
	if err != nil {
		return err
	}
	ct, err := crypto.Seal(kek, nonce, plaintextCred, s.aad(rec.AMSAccountID))
	if err != nil {
		return err
	}
	rec.Algorithm = int32(1) // ENCRYPTION_ALGORITHM_AES_256_GCM
	rec.KeyID = keyID
	rec.Nonce = nonce
	rec.Ciphertext = ct
	// Stamp the identity fingerprint from the plaintext being sealed so every
	// writer (deliver AND re-sync) leaves a correct detection baseline (§5.7).
	rec.Fingerprint = CredentialFingerprint(plaintextCred)
	if rec.ReceivedAt.IsZero() {
		rec.ReceivedAt = time.Now().UTC()
	}
	cp := rec
	s.records[rec.AMSAccountID] = &cp
	return s.persist()
}

// UpdateBaseline advances the detection baseline of an ALREADY-PRESENT record
// after an O9 re-sync push is accepted: it re-seals plaintextCred under the
// active KEK and restamps the envelope + fingerprint, but leaves AllocationStatus
// (and every other metadata field) untouched, and does nothing at all when the
// record is absent. This is deliberately NOT Upsert: Upsert runs outside the
// engine lock after the network Send, and in that window a concurrent recall may
// have deleted the record (purge=true) or flipped it to inactive (purge=false).
// A blind re-insert would resurrect a purged account or revive an inactive one,
// and reconcile would then re-inject a recalled credential. Returning ErrNotFound
// for an absent record lets the caller skip silently (the record is gone, so
// there is nothing left to keep a baseline for). The caller MUST wipe
// plaintextCred afterwards.
func (s *Store) UpdateBaseline(amsAccountID string, plaintextCred []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.records[amsAccountID]
	if !ok {
		// Deleted out from under us by a concurrent recall/purge — do not recreate.
		return ErrNotFound
	}
	kek, keyID, ok := s.keks.ActiveKey()
	if !ok {
		return ErrNoKEK
	}
	defer wipe(kek)
	nonce, err := crypto.NewNonce()
	if err != nil {
		return err
	}
	ct, err := crypto.Seal(kek, nonce, plaintextCred, s.aad(amsAccountID))
	if err != nil {
		return err
	}
	// Envelope + fingerprint only; AllocationStatus, Email, ReceivedAt, etc. are
	// preserved so a racing recall's status change survives.
	r.Algorithm = int32(1) // ENCRYPTION_ALGORITHM_AES_256_GCM
	r.KeyID = keyID
	r.Nonce = nonce
	r.Ciphertext = ct
	r.Fingerprint = CredentialFingerprint(plaintextCred)
	return s.persist()
}

// OpenCredential decrypts and returns the credential set for amsAccountID. The
// caller MUST wipe the returned bytes when done.
func (s *Store) OpenCredential(amsAccountID string) ([]byte, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	rec, ok := s.records[amsAccountID]
	if !ok {
		return nil, ErrNotFound
	}
	kek, ok := s.keks.Get(rec.KeyID)
	if !ok {
		return nil, ErrNoKEK
	}
	defer wipe(kek)
	return crypto.Open(kek, rec.Nonce, rec.Ciphertext, s.aad(amsAccountID))
}

// Get returns a shallow copy of the record for amsAccountID.
func (s *Store) Get(amsAccountID string) (Record, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.records[amsAccountID]
	if !ok {
		return Record{}, false
	}
	return *r, true
}

// FindByEmail returns a shallow copy of the record whose Email matches (the O9
// re-sync maps the live active account, known only by email, back to its
// ams_account_id and fingerprint baseline). Absent -> ok=false.
func (s *Store) FindByEmail(email string) (Record, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if email == "" {
		return Record{}, false
	}
	for _, r := range s.records {
		if r.Email == email {
			return *r, true
		}
	}
	return Record{}, false
}

// SetStatus updates a record's allocation status (e.g. recall=disable keeps the
// record and only marks it inactive — SSOT §6.2 / design note O2).
func (s *Store) SetStatus(amsAccountID string, status int32) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.records[amsAccountID]
	if !ok {
		return ErrNotFound
	}
	r.AllocationStatus = status
	return s.persist()
}

// Remove deletes a record entirely (recall with purge_local_copy=true).
func (s *Store) Remove(amsAccountID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.records[amsAccountID]; !ok {
		return nil // idempotent: absent is success
	}
	delete(s.records, amsAccountID)
	return s.persist()
}

// List returns shallow copies of every record, ordered by amsAccountId.
func (s *Store) List() []Record {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Record, 0, len(s.records))
	for _, r := range s.records {
		out = append(out, *r)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].AMSAccountID < out[j].AMSAccountID })
	return out
}

func wipe(b []byte) {
	for i := range b {
		b[i] = 0
	}
}

// atomicWrite writes b to path via a temp file + rename, with the given perms.
func atomicWrite(path string, b []byte, perm os.FileMode) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".tmp-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if err := tmp.Chmod(perm); err != nil {
		tmp.Close()
		return err
	}
	if _, err := tmp.Write(b); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpName, path)
}
