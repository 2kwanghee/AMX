// Package profile implements the P2 profile store (design note §2, §3 P2):
// one independent config home per account, laid out as
// <stateDir>/profiles/<provider>/<accountKey>/. It exists to give the future
// native seat engine (P3+) a switching primitive that never touches a live
// credential file in place — activation is defined as flipping a pointer, not
// swapping bytes — so the foreign/alien/wiped classification tsamx needs for
// its live-swap design has no equivalent problem to solve here.
//
// This package owns none of the vendor-specific credential knowledge: every
// operation that touches a credential's bytes or a config home's layout is
// delegated to the caller's provider.Driver (StageCredential, Fingerprint,
// Env, CredentialPath). Store only owns the directory layout, the per-profile
// lock, and the active pointer.
//
// P2 is deliberately inert: nothing in cmd/ama or the tsamx bridge constructs
// a Store. Wiring belongs to P3.
package profile

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/fslock"
	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
)

// profilesSubdir keeps this package's on-disk footprint under its own
// subdirectory of the agent's state root, so it never shares a file with
// store.Store's manifest.enc/applied.log or reporter's outbox (all siblings
// under AMX_STATE_DIR — see cmd/ama/main.go).
const profilesSubdir = "profiles"

// activeFileName holds the accountKey of the profile currently selected for a
// provider. It lives as a sibling of the account directories
// (<providersDir>/<provider>/active), never inside a profile itself, so
// removing a profile can never delete the pointer that might still name it.
const activeFileName = "active"

// lockFileName is the per-profile advisory lock, deliberately distinct from
// the deliver lock (`<configDir>/.amx-deliver.lock`, agent<->runner-wrapper
// contract — internal/fslock doc, tsamx-rewrite-feasibility.md contract
// table). Both files can coexist in the same config-home directory; they
// guard different critical sections (this package's Stage/Remove vs. the
// runner-launch/deliver race) and neither package reads the other's lock.
const lockFileName = ".amx-profile.lock"

// lockRetryBound/lockRetryInterval bound how long Stage/Remove wait on a
// contended profile lock before giving up. Contention here means another
// Stage/Remove on the very same profile is already in flight — normally a
// fast credential write or an rm -rf — not a long-held resource, so a short
// bounded retry (rather than tsamx-deliver's fail-open, or an unbounded
// block) is the right shape: give a genuinely racing caller a real chance,
// but never hang a command handler on a stuck lock file.
const (
	lockRetryBound    = 1 * time.Second
	lockRetryInterval = 20 * time.Millisecond
)

// Errors returned by GetActive. They are deliberately distinct: ErrNoActive
// means the provider has never had an active profile selected (or P3 hasn't
// run yet); ErrActiveMissing means a selection exists but the profile it
// names is gone (removed out from under the pointer) — the two call for
// different recovery (pick one vs. re-provision/alert).
var (
	// ErrNoActive means no active pointer has ever been written for the provider.
	ErrNoActive = errors.New("profile: no active profile set")
	// ErrActiveMissing means the active pointer names a profile that no longer exists.
	ErrActiveMissing = errors.New("profile: active profile no longer exists")
)

// Template describes the common, account-independent config files a new
// profile should be seeded with (settings.json, hooks, skill paths — design
// note §2's "공통 설정"). Deliberately caller-supplied rather than a
// hardcoded list: which files count as "common" is a policy question for
// whoever wires this store (P3), not this package's concern. The zero value
// (Dir == "" or Files == nil) copies nothing.
type Template struct {
	// Dir is the shared template directory. Empty disables the hook entirely.
	Dir string
	// Files are paths relative to Dir. Each is copied to the same relative
	// path under the new profile UNLESS the destination already exists (a
	// re-Create on an already-provisioned profile never clobbers a file the
	// account may have modified since) or the source is absent in Dir (a
	// caller may list files optimistically; a missing one is skipped, not an
	// error — design note "템플릿이 없으면 조용히 건너뛴다").
	Files []string
}

// Store is the on-disk profile layout under <stateDir>/profiles/. All
// operations are safe for concurrent use; Stage and Remove additionally take
// a per-profile file lock so they never interleave with each other on the
// same profile.
type Store struct {
	root string // <stateDir>/profiles
}

// Open ensures <stateDir>/profiles exists and returns a Store over it.
func Open(stateDir string) (*Store, error) {
	if stateDir == "" {
		return nil, errors.New("profile: empty state dir")
	}
	root := filepath.Join(stateDir, profilesSubdir)
	if err := os.MkdirAll(root, 0o700); err != nil {
		return nil, err
	}
	return &Store{root: root}, nil
}

// AccountKey derives the filesystem-safe, deterministic directory name for an
// account's email: hex(sha256(lowercase(trim(email)))).
//
//   - Deterministic: the same email always derives the same key, so a caller
//     can compute it independently of Store (e.g. to resolve a profile path
//     before Create has ever run).
//   - Collision-free in practice: a cryptographic hash's collision
//     probability is negligible next to the account counts this system will
//     ever see, unlike e.g. truncating or slugifying the local part.
//   - Case-insensitive: lowercasing before hashing merges "User@x.com" and
//     "user@X.com" into the same profile, matching how mailbox identity is
//     actually compared upstream (AMS/Claude account emails are not
//     case-sensitive identities) and avoiding a same-account split purely
//     from capitalization.
//   - Filesystem-safe and length-bounded: hex digest is exactly 64 lowercase
//     ASCII hex characters — no '@', no path separators, no Unicode
//     normalization pitfalls, no OS path-length concern regardless of how
//     long the source email is.
func AccountKey(email string) string {
	norm := strings.ToLower(strings.TrimSpace(email))
	sum := sha256.Sum256([]byte(norm))
	return hex.EncodeToString(sum[:])
}

// providerDir is <root>/<provider>, normalizing an empty provider key to
// provider.DefaultProvider the same way store.Store and reporter do.
func (s *Store) providerDir(providerKey string) string {
	return filepath.Join(s.root, provider.Normalize(providerKey))
}

// ProfileDir is <root>/<provider>/<accountKey> — the config-home directory a
// provider.Driver's CredentialPath/StageCredential/Env are called against.
// Exposed so a caller (P3's Switcher, tests) can resolve the path of a
// profile it has not necessarily created yet.
func (s *Store) ProfileDir(providerKey, accountKey string) string {
	return filepath.Join(s.providerDir(providerKey), accountKey)
}

// Create provisions the profile directory (0700) for (providerKey,
// accountKey) and, when tmpl names files, seeds the common-config files that
// do not already exist there. Idempotent: calling it again on an
// already-provisioned profile only fills in template files still missing: it
// never truncates or overwrites what is already on disk (a live credential,
// an account-modified settings.json). Returns the profile's config-home path.
func (s *Store) Create(providerKey, accountKey string, tmpl Template) (string, error) {
	if accountKey == "" {
		return "", errors.New("profile: empty accountKey")
	}
	dir := s.ProfileDir(providerKey, accountKey)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	if tmpl.Dir != "" {
		for _, rel := range tmpl.Files {
			if err := copyTemplateFile(tmpl.Dir, dir, rel); err != nil {
				return "", fmt.Errorf("profile: template file %q: %w", rel, err)
			}
		}
	}
	return dir, nil
}

// Stage writes credentialJSON (and any identity fields in meta) into the
// profile for (drv.Name(), accountKey), delegating the vendor-specific file
// layout to drv.StageCredential. It provisions the profile directory first
// (MkdirAll is idempotent; drv.StageCredential also MkdirAlls its configDir,
// so this is belt-and-suspenders, not the only guarantee) so the profile lock
// below always has a directory to live in, then holds the per-profile lock
// for the duration of the write so a concurrent Remove of the same profile
// cannot race it. credentialJSON is plaintext and MUST NEVER be logged by
// this package or its caller (§7 elsewhere in this repo's security notes).
func (s *Store) Stage(drv provider.Driver, accountKey string, credentialJSON []byte, meta provider.AddMeta) error {
	if accountKey == "" {
		return errors.New("profile: empty accountKey")
	}
	providerKey := drv.Name()
	dir := s.ProfileDir(providerKey, accountKey)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	lock, err := s.lock(providerKey, accountKey)
	if err != nil {
		return err
	}
	defer lock.Unlock()
	return drv.StageCredential(dir, credentialJSON, meta)
}

// Remove deletes the profile for (providerKey, accountKey). Idempotent: a
// profile that does not exist is success, matching store.Store.Remove's
// convention elsewhere in this repo. It does NOT touch the active pointer —
// a caller that removes the currently-active profile is left with a pointer
// GetActive will report as ErrActiveMissing, which is the explicit signal P3
// needs to notice and re-point rather than this package silently guessing a
// replacement.
func (s *Store) Remove(providerKey, accountKey string) error {
	if accountKey == "" {
		return errors.New("profile: empty accountKey")
	}
	dir := s.ProfileDir(providerKey, accountKey)
	if _, err := os.Stat(dir); err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil // idempotent: absent is success
		}
		return err
	}
	lock, err := s.lock(providerKey, accountKey)
	if err != nil {
		return err
	}
	defer lock.Unlock()
	return os.RemoveAll(dir)
}

// List enumerates the accountKeys of profiles that currently exist for
// providerKey, sorted. A provider with no profiles yet (directory absent)
// returns an empty, non-nil slice rather than an error.
func (s *Store) List(providerKey string) ([]string, error) {
	dir := s.providerDir(providerKey)
	entries, err := os.ReadDir(dir)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return []string{}, nil
		}
		return nil, err
	}
	keys := make([]string, 0, len(entries))
	for _, e := range entries {
		// Only account directories are profiles; activeFileName and the lock
		// files (which live one level down, inside each profile dir, never
		// here) are the only non-directory entries this level can hold.
		if e.IsDir() {
			keys = append(keys, e.Name())
		}
	}
	sort.Strings(keys)
	return keys, nil
}

// Fingerprint reads the live credential file for (drv.Name(), accountKey)
// via drv.CredentialPath and returns drv.Fingerprint of its bytes — the same
// identity hash rule the manifest store and O9 re-sync use, delegated rather
// than reimplemented (design note P2: "지문 규칙을 재구현하지 말고 드라이버에
// 위임"). The plaintext credential bytes are wiped before returning; the
// fingerprint itself is a one-way hash, safe to return and log.
func (s *Store) Fingerprint(drv provider.Driver, accountKey string) (string, error) {
	dir := s.ProfileDir(drv.Name(), accountKey)
	b, err := os.ReadFile(drv.CredentialPath(dir))
	if err != nil {
		return "", err
	}
	defer wipe(b)
	return drv.Fingerprint(b), nil
}

// Env returns the process environment entries (drv.Env) that point the
// vendor's pool binary at this profile's config home.
func (s *Store) Env(drv provider.Driver, accountKey string) []string {
	return drv.Env(s.ProfileDir(drv.Name(), accountKey))
}

// SetActive atomically records accountKey as the active profile for
// providerKey. It does not verify the profile exists — P3's Switcher is
// expected to Create/Stage before pointing at it — so GetActive is where
// "points at nothing" is caught and reported as ErrActiveMissing.
func (s *Store) SetActive(providerKey, accountKey string) error {
	if accountKey == "" {
		return errors.New("profile: empty accountKey")
	}
	dir := s.providerDir(providerKey)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	return atomicWrite(filepath.Join(dir, activeFileName), []byte(accountKey), 0o600)
}

// GetActive reads the active pointer for providerKey. It returns
// (accountKey, configDir, nil) when the pointer names a profile that exists;
// ("", "", ErrNoActive) when no pointer has been written; and (accountKey,
// "", ErrActiveMissing) when a pointer exists but the profile it names does
// not — accountKey is still returned in that case so a caller can log which
// key went missing.
func (s *Store) GetActive(providerKey string) (accountKey, configDir string, err error) {
	dir := s.providerDir(providerKey)
	raw, rerr := os.ReadFile(filepath.Join(dir, activeFileName))
	if rerr != nil {
		if errors.Is(rerr, fs.ErrNotExist) {
			return "", "", ErrNoActive
		}
		return "", "", rerr
	}
	key := strings.TrimSpace(string(raw))
	if key == "" {
		return "", "", ErrNoActive
	}
	profDir := filepath.Join(dir, key)
	if _, serr := os.Stat(profDir); serr != nil {
		if errors.Is(serr, fs.ErrNotExist) {
			return key, "", ErrActiveMissing
		}
		return key, "", serr
	}
	return key, profDir, nil
}

// lock acquires the per-profile advisory lock for (providerKey, accountKey),
// retrying on contention up to lockRetryBound. The profile directory MUST
// already exist (fslock.TryLock cannot create a lock file under a missing
// parent); every caller in this file creates it first.
func (s *Store) lock(providerKey, accountKey string) (*fslock.Lock, error) {
	path := filepath.Join(s.ProfileDir(providerKey, accountKey), lockFileName)
	deadline := time.Now().Add(lockRetryBound)
	for {
		l, err := fslock.TryLock(path)
		if err == nil {
			return l, nil
		}
		if !errors.Is(err, fslock.ErrWouldBlock) || time.Now().After(deadline) {
			return nil, fmt.Errorf("profile: lock %s/%s held: %w", providerKey, accountKey, err)
		}
		time.Sleep(lockRetryInterval)
	}
}

// copyTemplateFile copies <srcDir>/<rel> to <dstDir>/<rel> unless the source
// is absent (silently skipped — a template need not carry every file a
// caller optimistically lists) or the destination already exists (never
// clobber a file the profile may already have, staged or account-modified).
func copyTemplateFile(srcDir, dstDir, rel string) error {
	src := filepath.Join(srcDir, rel)
	info, err := os.Stat(src)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil
		}
		return err
	}
	if info.IsDir() {
		return nil // template Files names config files, not subtrees
	}
	dst := filepath.Join(dstDir, rel)
	if _, err := os.Stat(dst); err == nil {
		return nil // already provisioned
	} else if !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0o700); err != nil {
		return err
	}
	data, err := os.ReadFile(src)
	if err != nil {
		return err
	}
	return atomicWrite(dst, data, 0o600)
}

// wipe zeroes b in place. Used after a credential's plaintext bytes have
// served their purpose (Fingerprint), mirroring internal/store's convention
// for anything that briefly holds secret material.
func wipe(b []byte) {
	for i := range b {
		b[i] = 0
	}
}

// atomicWrite writes data to path via a temp file in the same directory +
// rename, so a concurrent reader never observes a partial write. Mirrors
// internal/store.atomicWrite and internal/provider/claude.writeFileAtomic
// (duplicated rather than shared: each package's copy is small, private, and
// this one deliberately depends on nothing outside stdlib + this file).
func atomicWrite(path string, data []byte, perm os.FileMode) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".amx-profile-*.tmp")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer func() {
		if tmpName != "" {
			_ = os.Remove(tmpName)
		}
	}()
	if err := tmp.Chmod(perm); err != nil {
		_ = tmp.Close()
		return err
	}
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmpName, path); err != nil {
		return err
	}
	tmpName = ""
	return nil
}
