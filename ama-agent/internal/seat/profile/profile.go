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
// Every public entry point that takes a providerKey/accountKey validates both
// against a strict whitelist before touching the filesystem (see
// validateProviderKey/validateAccountKey) and rejects either the provider or
// profile directory being a symlink (rejectSymlink). Neither check is
// airtight against a TOCTOU race with something else modifying the state
// tree between the check and the use — that would need OS-level
// no-follow-symlink open primitives this package does not use — but it does
// close the concrete path-escape and symlink-redirection bugs an adversarial
// review found in an earlier revision (a "." or ".." accountKey deleting the
// whole state dir; a pre-planted symlink redirecting a Stage write outside
// the store root).
//
// P2 was deliberately inert: nothing in cmd/ama or the tsamx bridge
// constructed a Store. P3 (design note §3 P3) wires a Switcher on top of this
// package and resolves the P2 review items that were left open here:
//   - accountKey -> email lookup (P2 review ②): AccountKey is one-way, so a
//     caller holding only an accountKey (e.g. from GetActive) cannot recover
//     the email it hashes without re-reading something StageCredential wrote.
//     Resolved via provider.Driver.Identity(configDir), NOT by this package
//     parsing a vendor file itself (that would break the "owns no
//     vendor-specific knowledge" rule two paragraphs up). Identity reads back
//     the RAW email StageCredential staged — the same, un-normalized string
//     the manifest store keys on — so a caller resolving a profile back to a
//     manifest record via store.Store.FindByProviderEmail (case-sensitive
//     exact match) does not have to reconcile it against AccountKey's
//     lowercase-before-hash normalization (P2 review ⑥, first half).
//   - marker rewrite on observed rotation (P2 review ①): State/Reconcile
//     below, not a change to Complete (kept for its pinned tests — see its
//     doc).
//   - orphaned active-pointer cleanup (P2 review ④) and owner-scope
//     enforcement (P2 review ⑤): both live in internal/seat's Switcher, one
//     layer up, not in this package — Switcher is the only intended caller of
//     SetActive going forward.
//   - unifying this package's per-profile lock with the deliver lock
//     (`<configDir>/.amx-deliver.lock`) is NOT attempted: P3 instead defines
//     the deliver lock's anchor independently of any active profile (see
//     provider.Driver.DefaultConfigHome and internal/tsamx/exec.go's
//     lockConfigHome) so the two locks stay correct without ever needing to
//     become the same file.
//
// NOT resolved by P3, deliberately: P2 review ⑥'s second half — tsamx keys a
// slot by (email, organizationUuid) while AccountKey hashes only the email,
// so a personal and an organization account sharing one email collapse onto
// the SAME profile directory here. Changing AccountKey's shape is out of
// scope for a P3-sized change (it would ripple through every already-shipped
// P2 test and the on-disk layout this package's callers depend on); the risk
// is carried forward and must be closed before this store is used for an
// account population where that collision is plausible.
package profile

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"crypto/sha256"
	"encoding/hex"

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

// stagedMarkerName is written into a profile directory ONLY after
// drv.StageCredential has returned successfully (see Stage/Complete). Its
// presence is the sole signal that a profile's credential write finished
// rather than died partway through.
const stagedMarkerName = ".amx-profile-staged"

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

// providerKeyPattern whitelists the provider path component: lowercase
// letters/digits/dot/underscore/dash, no separators of any kind. accountKeyPattern
// matches exactly the shape AccountKey produces (64 lowercase hex chars); any
// value that isn't shaped like a real AccountKey output is rejected rather
// than trusted, which is what makes a hand-crafted accountKey like "." or
// "../.." impossible to reach the filesystem layer at all.
var (
	providerKeyPattern = regexp.MustCompile(`^[a-z0-9._-]+$`)
	accountKeyPattern  = regexp.MustCompile(`^[0-9a-f]{64}$`)
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
	// error — design note "템플릿이 없으면 조용히 건너뛴다"). A path that is
	// absolute, escapes the profile directory via "..", or names the
	// reserved staged-marker file is rejected as an error, not skipped —
	// unlike a merely-absent source file, that shape is always a caller bug.
	Files []string
}

// Store is the on-disk profile layout under <stateDir>/profiles/. All
// operations are safe for concurrent use; Stage and Remove additionally take
// a per-profile file lock so they never interleave with each other on the
// same profile.
type Store struct {
	root string // <stateDir>/profiles
}

// Open ensures <stateDir>/profiles exists and returns a Store over it. It
// rejects a pre-existing root that is a symlink (N1: without this, a symlink
// planted at <stateDir>/profiles before Open is ever called defeats every
// other check in this file, since providerDir/profileDir are then real
// directories AT THE SYMLINK'S TARGET and none of the per-level rejectSymlink
// calls below ever look at root itself) and tightens root's mode to 0700 if
// it already existed wider.
func Open(stateDir string) (*Store, error) {
	if stateDir == "" {
		return nil, errors.New("profile: empty state dir")
	}
	root := filepath.Join(stateDir, profilesSubdir)
	if err := rejectSymlink(root); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(root, 0o700); err != nil {
		return nil, err
	}
	if err := ensureDirPerm(root); err != nil {
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
//
// KNOWN UNRESOLVED MISMATCH (P3 must resolve, not fixed here):
// internal/store.Store.FindByProviderEmail (manifest.go) matches an account's
// email by case-sensitive exact equality, while this function lowercases
// before hashing. Two manifest records that differ only by email case are
// distinct accounts to the manifest but collapse onto the SAME profile here.
// This package deliberately does not change manifest matching (out of
// scope, and it isn't this package's file to own); whoever wires this store
// to real account flows must decide how the two normalization rules
// reconcile before an email-case mismatch can silently cross-wire two
// accounts' credentials into one profile.
func AccountKey(email string) string {
	norm := strings.ToLower(strings.TrimSpace(email))
	sum := sha256.Sum256([]byte(norm))
	return hex.EncodeToString(sum[:])
}

// validateProviderKey normalizes an empty providerKey to provider.DefaultProvider
// (matching store.Store/reporter convention) and rejects anything that is not
// a bare, separator-free path component — in particular the literal "." and
// ".." are rejected even though they would otherwise match the charset.
func validateProviderKey(raw string) (string, error) {
	key := provider.Normalize(raw)
	if key == "." || key == ".." || !providerKeyPattern.MatchString(key) {
		return "", fmt.Errorf("profile: invalid provider key %q", raw)
	}
	return key, nil
}

// validateAccountKey rejects any accountKey that is not exactly the shape
// AccountKey produces. This is the single check that makes path-escape
// accountKeys like "..", "../..", or an absolute path impossible to reach
// filepath.Join at all: none of them can ever match 64 lowercase hex chars.
func validateAccountKey(key string) error {
	if !accountKeyPattern.MatchString(key) {
		return fmt.Errorf("profile: invalid account key %q", key)
	}
	return nil
}

// rejectSymlink lstat's path and errors if it exists and is a symlink rather
// than a real entry. A symlinked provider or profile directory would make
// every subsequent read/write land wherever the link points, not under
// s.root, silently defeating the path whitelist above. A path that does not
// exist yet is not rejected — Create/Stage's MkdirAll is what will bring it
// into existence as a real directory.
func rejectSymlink(path string) error {
	info, err := os.Lstat(path)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil
		}
		return err
	}
	if info.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("profile: %s is a symlink, refusing to use it", path)
	}
	return nil
}

// ensureDirPerm re-lstat's an EXISTING directory and tightens its mode to
// 0700 if a caller (or an old profile from before this check existed) left it
// wider. Chosen over rejecting outright: this Store is the exclusive owner of
// every path under its root, so correcting the mode is safe, and rejecting
// would turn a stray pre-existing 0777 directory (a slow deploy script, a
// leftover from before this fix shipped) into a permanent failure instead of
// a one-time self-heal. It re-checks for a symlink too, since a symlink could
// have been swapped in between an earlier rejectSymlink check and this call.
func ensureDirPerm(dir string) error {
	info, err := os.Lstat(dir)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil
		}
		return err
	}
	if info.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("profile: %s is a symlink, refusing to use it", dir)
	}
	if !info.IsDir() {
		return fmt.Errorf("profile: %s exists and is not a directory", dir)
	}
	if info.Mode().Perm() != 0o700 {
		if err := os.Chmod(dir, 0o700); err != nil {
			return err
		}
	}
	return nil
}

// resolveProviderDir validates providerKey and returns <root>/<provider>,
// having rejected it being a symlink.
func (s *Store) resolveProviderDir(providerKey string) (string, error) {
	key, err := validateProviderKey(providerKey)
	if err != nil {
		return "", err
	}
	dir := filepath.Join(s.root, key)
	if err := rejectSymlink(dir); err != nil {
		return "", err
	}
	return dir, nil
}

// resolveProfile validates both providerKey and accountKey and returns
// (profileDir, providerDir), having rejected either level being a symlink.
// Every Create/Stage/Remove/Fingerprint/Env/ProfileDir/Complete call funnels
// through this before touching the filesystem.
func (s *Store) resolveProfile(providerKey, accountKey string) (profileDir, providerDir string, err error) {
	providerDir, err = s.resolveProviderDir(providerKey)
	if err != nil {
		return "", "", err
	}
	if err := validateAccountKey(accountKey); err != nil {
		return "", "", err
	}
	profileDir = filepath.Join(providerDir, accountKey)
	if err := rejectSymlink(profileDir); err != nil {
		return "", "", err
	}
	return profileDir, providerDir, nil
}

// confirmWithinRoot is a second, independent check immediately before the
// one destructive operation this package has (Remove's os.RemoveAll): it
// resolves BOTH dir and s.root through filepath.EvalSymlinks (not a pure
// lexical filepath.Abs — a lexical-only comparison passes trivially whenever
// dir was built by joining s.root with more path segments, string-consistent
// with itself even if s.root has, since Open ran, come to be reached through
// a symlink) and refuses to proceed unless the resolved dir falls strictly
// under the resolved root. The boundary check appends the OS separator to
// the prefix so "/root-evil" is never mistaken for a child of "/root".
//
// Both EvalSymlinks calls require their argument to already exist:
// s.root always does (Open created it and this package never removes it);
// dir is guaranteed to exist here because every caller (only Remove) has
// already run a successful os.Stat(dir) immediately before calling this. A
// caller that ever changed that ordering would see EvalSymlinks fail with
// ErrNotExist, which this function treats as a hard error (fail closed —
// refuse to remove) rather than silently skipping the check.
func (s *Store) confirmWithinRoot(dir string) error {
	absRoot, err := filepath.Abs(s.root)
	if err != nil {
		return err
	}
	resolvedRoot, err := filepath.EvalSymlinks(absRoot)
	if err != nil {
		return fmt.Errorf("profile: resolve store root: %w", err)
	}
	absDir, err := filepath.Abs(dir)
	if err != nil {
		return err
	}
	resolvedDir, err := filepath.EvalSymlinks(absDir)
	if err != nil {
		return fmt.Errorf("profile: resolve target path: %w", err)
	}
	if resolvedDir != resolvedRoot && !strings.HasPrefix(resolvedDir, resolvedRoot+string(filepath.Separator)) {
		return fmt.Errorf("profile: refusing to remove path outside store root: %s", resolvedDir)
	}
	return nil
}

// ProfileDir returns the config-home path for (providerKey, accountKey)
// after validating both and rejecting a symlinked provider or profile
// directory. It does not require the profile to already exist.
func (s *Store) ProfileDir(providerKey, accountKey string) (string, error) {
	dir, _, err := s.resolveProfile(providerKey, accountKey)
	return dir, err
}

// Create provisions the profile directory (0700) for (providerKey,
// accountKey) and, when tmpl names files, seeds the common-config files that
// do not already exist there. Idempotent: calling it again on an
// already-provisioned profile only fills in template files still missing: it
// never truncates or overwrites what is already on disk (a live credential,
// an account-modified settings.json), and it tightens the provider/profile
// directory mode back to 0700 if either was left wider. Returns the
// profile's config-home path.
func (s *Store) Create(providerKey, accountKey string, tmpl Template) (string, error) {
	dir, providerDir, err := s.resolveProfile(providerKey, accountKey)
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	if err := ensureDirPerm(providerDir); err != nil {
		return "", err
	}
	if err := ensureDirPerm(dir); err != nil {
		return "", err
	}
	if tmpl.Dir != "" {
		for _, rel := range tmpl.Files {
			if err := validateTemplateRel(rel); err != nil {
				return "", fmt.Errorf("profile: template file %q: %w", rel, err)
			}
			if err := copyTemplateFile(tmpl.Dir, dir, rel); err != nil {
				return "", fmt.Errorf("profile: template file %q: %w", rel, err)
			}
		}
	}
	return dir, nil
}

// Stage writes credentialJSON (and any identity fields in meta) into the
// profile for (drv.Name(), accountKey), delegating the vendor-specific file
// layout to drv.StageCredential, then writes a staged marker recording
// drv.Fingerprint(credentialJSON) — ONLY after StageCredential returns
// without error, so Complete()==true means the write finished AND the bytes
// on disk are still the ones Stage put there. It is not a claim that the
// credential is usable: a later in-place rotation by the vendor's runner
// moves the fingerprint and flips Complete to false (see Complete).
//
// The FIRST thing Stage does after acquiring the lock is delete any existing
// marker (N2): a re-Stage of an already-complete profile (a rotated
// credential being re-delivered) that dies partway — inside
// drv.StageCredential itself, which is not one atomic operation but at least
// two file writes for the claude driver — must NOT leave the OLD marker
// standing next to a NEW, possibly-partial on-disk credential. Deleting the
// marker up front means any death before the final atomicWrite below leaves
// Complete()==false, matching what is actually on disk, instead of a stale
// "complete" from the credential this Stage was replacing.
//
// It provisions and permission-tightens the profile directory first (see
// Create) so the profile lock below always has a real, exclusively-owned
// directory to live in, then holds the per-profile lock for the duration of
// the write so a concurrent Remove of the same profile cannot race it.
// credentialJSON is plaintext and MUST NEVER be logged by this package or
// its caller (§7 elsewhere in this repo's security notes).
//
// N7 (deliberately NOT fixed): there is a TOCTOU window between
// ensureDirPerm's checks above and drv.StageCredential's own writes below —
// something could in principle replace a path component in that gap. Closing
// it for real would need file-descriptor-relative (openat2-style,
// no-follow-symlink) primitives this package does not use anywhere, which is
// a bigger redesign than this fix pass; the window is accepted, not solved.
func (s *Store) Stage(drv provider.Driver, accountKey string, credentialJSON []byte, meta provider.AddMeta) error {
	dir, providerDir, err := s.resolveProfile(drv.Name(), accountKey)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	if err := ensureDirPerm(providerDir); err != nil {
		return err
	}
	if err := ensureDirPerm(dir); err != nil {
		return err
	}
	lock, err := s.lock(providerDir, accountKey)
	if err != nil {
		return err
	}
	defer lock.Unlock()
	markerPath := filepath.Join(dir, stagedMarkerName)
	if err := os.Remove(markerPath); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	if err := drv.StageCredential(dir, credentialJSON, meta); err != nil {
		return err
	}
	return atomicWrite(markerPath, []byte(drv.Fingerprint(credentialJSON)), 0o600)
}

// Complete reports whether the profile for (drv.Name(), accountKey) has a
// credential that finished staging AND still matches what Stage recorded:
// the marker Stage writes (see Stage) holds drv.Fingerprint of the
// credential bytes it just staged, and Complete recomputes the fingerprint
// of whatever is on disk RIGHT NOW and requires the two to match. This
// closes two gaps a marker-existence-only check left open (N2/N3):
//
//   - a re-Stage that died after invalidating the old marker but before
//     writing the new one leaves no marker at all -> false, correctly;
//   - a marker planted WITHOUT a matching credential file (or without any
//     credential file at all) reads as false: computing the live
//     fingerprint either fails outright or yields a value the planted
//     marker does not match. A marker planted TOGETHER with the credential
//     it names still reads as true — the fingerprint is an unkeyed hash, so
//     this detects truncation and drift, NOT forgery by someone who already
//     has write access inside the profile directory.
//
// A profile that is Create()d but never Stage()d, and a Stage that died
// mid-write, read as !Complete — the runner would hit a login screen.
//
// CAUTION (this is the exact trap State/Reconcile below exist to avoid): do
// NOT wire !Complete straight through to "not ready". A credential rotated
// IN PLACE by the vendor's own runner also reads as !Complete here, because
// the live bytes no longer match what Stage recorded. That is a HEALTHY
// account, not a broken one — this repo already handles local rotation as a
// normal event (see internal/resync). Treating every !Complete as not-ready
// would flip every rotated account to unusable. Complete is kept, unchanged,
// only because its existing tests pin its exact three-way marker/credential
// comparison; any NEW caller that needs a readiness judgment should call
// State (which distinguishes "no/short credential" from "credential present
// but fingerprint moved") and, on StateRotated, Reconcile (which re-records
// the marker) instead of calling Complete directly.
func (s *Store) Complete(drv provider.Driver, accountKey string) (bool, error) {
	dir, _, err := s.resolveProfile(drv.Name(), accountKey)
	if err != nil {
		return false, err
	}
	markerFP, err := os.ReadFile(filepath.Join(dir, stagedMarkerName))
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return false, nil
		}
		return false, err
	}
	credBytes, err := os.ReadFile(drv.CredentialPath(dir))
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return false, nil // marker exists but there is no credential to match it
		}
		return false, err
	}
	defer wipe(credBytes)
	liveFP := drv.Fingerprint(credBytes)
	return liveFP != "" && string(markerFP) == liveFP, nil
}

// State classifies a profile's credential readiness for a caller (P3's
// Switcher) that needs to tell "nothing usable was ever staged here" apart
// from "something usable is here, but a local rotation moved it out from
// under the marker Stage recorded" — the second case is a HEALTHY account
// (see Complete's CAUTION above), not a broken one.
type State int

const (
	// StateAbsent: no credential file exists at all for this profile — never
	// Stage()d, or wiped (e.g. the vendor's runner cleared it after a failed
	// refresh). Not ready; nothing to Reconcile.
	StateAbsent State = iota
	// StateIncomplete: a credential file exists but drv.HasCredentialMaterial
	// says it carries no usable token (a logged-out shell). Not ready; fixed
	// by a real re-login or re-Stage, not by a marker rewrite.
	StateIncomplete
	// StateStaged: a credential file exists, carries material, and its
	// fingerprint matches the marker Stage last recorded. Ready; no action
	// needed.
	StateStaged
	// StateRotated: a credential file exists and carries material, but its
	// fingerprint does not match the recorded marker (including the case
	// where no marker was ever recorded despite usable material being
	// present, e.g. a manual login into a Create()d-but-never-Stage()d
	// profile). This is the healthy in-place-rotation case — ready to use
	// as-is. Reconcile re-stamps the marker so a later State/Complete call
	// reads StateStaged.
	StateRotated
)

// String renders State for logs; never for filesystem paths or equality
// checks (compare the State value itself for those).
func (st State) String() string {
	switch st {
	case StateAbsent:
		return "absent"
	case StateIncomplete:
		return "incomplete"
	case StateStaged:
		return "staged"
	case StateRotated:
		return "rotated"
	default:
		return "unknown"
	}
}

// State reports the credential readiness of (drv.Name(), accountKey). It
// never writes anything (see Reconcile for the write path) and holds no
// lock, so a concurrent Stage/Remove on the same profile can change the
// answer between this read and a caller's next action — a caller that needs
// to act on StateRotated should call Reconcile directly rather than
// State-then-decide-then-Reconcile, since Reconcile re-verifies under its
// own lock instead of trusting a State call result that may already be
// stale by the time it is used.
func (s *Store) State(drv provider.Driver, accountKey string) (State, error) {
	dir, _, err := s.resolveProfile(drv.Name(), accountKey)
	if err != nil {
		return StateAbsent, err
	}
	return stateLocked(drv, dir)
}

// Reconcile observes the same readiness State reports and, when it is
// StateRotated, re-stamps the marker with the credential's CURRENT
// fingerprint — accepting the observed rotation as the new baseline — so a
// subsequent State/Complete call reads StateStaged. This is the "마커를
// 재기록하는 경로" the design note (P3, resolving P2 review item ①) asks for.
// StateAbsent/StateIncomplete are returned unchanged, with no write: there is
// nothing to reconcile when no usable credential is present. It takes the
// same per-profile lock Stage takes, so it can never interleave with a
// concurrent Stage/Remove of the same profile, and re-derives State fresh
// under that lock rather than trusting any State call a caller made earlier.
func (s *Store) Reconcile(drv provider.Driver, accountKey string) (State, error) {
	dir, providerDir, err := s.resolveProfile(drv.Name(), accountKey)
	if err != nil {
		return StateAbsent, err
	}
	lock, err := s.lock(providerDir, accountKey)
	if err != nil {
		return StateAbsent, err
	}
	defer lock.Unlock()

	st, credBytes, liveFP, err := stateWithFingerprint(drv, dir)
	if err != nil {
		return StateAbsent, err
	}
	defer wipe(credBytes)
	if st != StateRotated {
		return st, nil // absent/incomplete: nothing to reconcile; staged: already matches
	}
	if err := atomicWrite(filepath.Join(dir, stagedMarkerName), []byte(liveFP), 0o600); err != nil {
		return StateAbsent, err
	}
	return StateStaged, nil
}

// stateLocked is State's body, factored out so Reconcile can call the same
// classification under its own lock via stateWithFingerprint.
func stateLocked(drv provider.Driver, dir string) (State, error) {
	st, credBytes, _, err := stateWithFingerprint(drv, dir)
	wipe(credBytes)
	return st, err
}

// stateWithFingerprint reads the live credential at dir and classifies it,
// also returning the raw credential bytes (caller must wipe) and the live
// fingerprint (only meaningful when the returned State is StateRotated or
// StateStaged) so Reconcile does not need to re-read the credential file a
// second time under its lock.
func stateWithFingerprint(drv provider.Driver, dir string) (State, []byte, string, error) {
	credBytes, err := os.ReadFile(drv.CredentialPath(dir))
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return StateAbsent, nil, "", nil
		}
		return StateAbsent, nil, "", err
	}
	if !drv.HasCredentialMaterial(credBytes) {
		return StateIncomplete, credBytes, "", nil
	}
	liveFP := drv.Fingerprint(credBytes)
	markerFP, rerr := os.ReadFile(filepath.Join(dir, stagedMarkerName))
	if rerr != nil && !errors.Is(rerr, fs.ErrNotExist) {
		return StateAbsent, credBytes, "", rerr
	}
	if rerr == nil && liveFP != "" && string(markerFP) == liveFP {
		return StateStaged, credBytes, liveFP, nil
	}
	return StateRotated, credBytes, liveFP, nil
}

// Remove deletes the profile for (providerKey, accountKey). Idempotent: a
// profile that does not exist is success, matching store.Store.Remove's
// convention elsewhere in this repo. It does NOT touch the active pointer —
// a caller that removes the currently-active profile is left with a pointer
// GetActive will report as ErrActiveMissing, which is the explicit signal P3
// needs to notice and re-point rather than this package silently guessing a
// replacement.
//
// It deliberately does NOT delete the per-profile lock file afterward (see
// lockPath): the lock lives one level up as <providerDir>/<accountKey>.lock,
// outside the tree os.RemoveAll below deletes, so a successful Remove leaves
// one empty lock file behind per removed profile. Deleting it here would
// require unlinking it only after Unlock() (a lock still held cannot be
// unlinked safely on Windows), which reopens exactly the unlink-then-recreate
// race this file's lock placement exists to close — a caller racing to
// re-Stage the same accountKey could TryLock the same path in the gap between
// this Remove's Unlock and its own cleanup unlink. A handful of empty stray
// files is a cheap, static trade against reintroducing that race.
func (s *Store) Remove(providerKey, accountKey string) error {
	dir, providerDir, err := s.resolveProfile(providerKey, accountKey)
	if err != nil {
		return err
	}
	if _, err := os.Stat(dir); err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil // idempotent: absent is success
		}
		return err
	}
	if err := s.confirmWithinRoot(dir); err != nil {
		return err
	}
	lock, err := s.lock(providerDir, accountKey)
	if err != nil {
		return err
	}
	defer lock.Unlock()
	return os.RemoveAll(dir)
}

// List enumerates the accountKeys of profiles that currently exist for
// providerKey, sorted. A provider with no profiles yet (directory absent)
// returns an empty, non-nil slice rather than an error. It lists both
// Complete and not-yet-Complete profiles — a caller that only wants
// ready-to-use accounts must check Complete itself; the existing behaviour
// (List returns every provisioned directory) predates and is unrelated to
// the staged-marker addition, and changing it would make a Create()-only
// profile invisible to List, which nothing in this package's contract
// promises.
func (s *Store) List(providerKey string) ([]string, error) {
	dir, err := s.resolveProviderDir(providerKey)
	if err != nil {
		return nil, err
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return []string{}, nil
		}
		return nil, err
	}
	keys := make([]string, 0, len(entries))
	for _, e := range entries {
		// Only account directories shaped like a real AccountKey output are
		// profiles; activeFileName and the *.lock files (siblings at this
		// same level, never directories) are excluded by both conditions.
		if e.IsDir() && accountKeyPattern.MatchString(e.Name()) {
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
// 위임"). The credential bytes this function itself read are zeroed before
// returning (wipe), but that is a best-effort courtesy on this ONE []byte
// allocation, not a guarantee that no copy of the credential survives
// anywhere in memory: drv.Fingerprint (like drv.StageCredential) typically
// json.Unmarshals the bytes into a Go string field to reach the token, and a
// Go string is immutable and cannot be wiped by this or any other caller —
// that copy lives until the garbage collector reclaims it. The fingerprint
// this function returns is itself a one-way hash, so returning and logging
// IT is safe regardless.
func (s *Store) Fingerprint(drv provider.Driver, accountKey string) (string, error) {
	dir, _, err := s.resolveProfile(drv.Name(), accountKey)
	if err != nil {
		return "", err
	}
	b, err := os.ReadFile(drv.CredentialPath(dir))
	if err != nil {
		return "", err
	}
	defer wipe(b)
	return drv.Fingerprint(b), nil
}

// Env returns the process environment entries (drv.Env) that point the
// vendor's pool binary at this profile's config home.
func (s *Store) Env(drv provider.Driver, accountKey string) ([]string, error) {
	dir, _, err := s.resolveProfile(drv.Name(), accountKey)
	if err != nil {
		return nil, err
	}
	return drv.Env(dir), nil
}

// SetActive atomically records accountKey as the active profile for
// providerKey. It validates both like every other entry point but does NOT
// verify the profile exists — P3's Switcher is expected to Create/Stage
// before pointing at it — so GetActive is where "points at nothing" is
// caught and reported as ErrActiveMissing. Like Create/Stage it tightens the
// provider directory back to 0700 if it was left wider (N6: this was
// previously only self-healed by Create/Stage, so a provider directory that
// had never seen either — only SetActive — stayed at whatever mode MkdirAll
// happened to leave it, or wider if pre-planted).
func (s *Store) SetActive(providerKey, accountKey string) error {
	if err := validateAccountKey(accountKey); err != nil {
		return err
	}
	dir, err := s.resolveProviderDir(providerKey)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	if err := ensureDirPerm(dir); err != nil {
		return err
	}
	return atomicWrite(filepath.Join(dir, activeFileName), []byte(accountKey), 0o600)
}

// GetActive reads the active pointer for providerKey. It returns
// (accountKey, configDir, nil) when the pointer names a profile that exists;
// ("", "", ErrNoActive) when no pointer has been written; (accountKey, "",
// ErrActiveMissing) when a pointer exists but the profile it names does not
// (accountKey is still returned so a caller can log which key went missing);
// and (key, "", err) when the pointer's raw content is not shaped like a
// real AccountKey output at all — a distinct failure from ErrActiveMissing,
// since that content could not have come from a legitimate SetActive call
// and is never used to build a path.
//
// POINTER FORMAT — canonical rule (adversarial review F2; this is the ONE
// place this rule is defined, and it MUST match the independent
// reimplementation in deploy/amx-claude's active-pointer case pattern
// byte-for-byte, or the two readers disagree about which profile is active —
// which is exactly the "runner bills one account, [a reader elsewhere]
// attributes the cost to another" bug F2 reproduced): the file's content must
// be EITHER exactly `^[0-9a-f]{64}$` with nothing else, OR that same
// 64-hex-char string followed by exactly one trailing "\n" and nothing after
// it. Anything else — leading whitespace, internal whitespace, a trailing
// "\r", two or more trailing newlines, any other stray byte — is rejected as
// an invalid pointer, NOT silently trimmed. atomicWrite (see SetActive) never
// appends a trailing newline on this package's own write path, so the
// "\n"-tolerant branch exists only for a pointer file some other tool wrote
// by hand. strings.TrimSuffix removes AT MOST one trailing "\n" (never more),
// which is what makes this the correct primitive here — strings.TrimSpace
// would strip leading whitespace and multiple trailing newlines too, silently
// ACCEPTING malformed pointers the other reader rejects (the exact F2 bug).
//
// deploy/langfuse/session_usage_hook.py used to be a third independent
// reimplementation of this same parsing, but adversarial review F3 removed
// its pointer re-read entirely (it now reads the session's own
// CLAUDE_CONFIG_DIR instead of asking "what's active right now" — see that
// file's _session_config_home_email doc) — so it is no longer a party to
// this rule at all, not a third place that must be kept in sync with it.
func (s *Store) GetActive(providerKey string) (accountKey, configDir string, err error) {
	dir, err := s.resolveProviderDir(providerKey)
	if err != nil {
		return "", "", err
	}
	raw, rerr := os.ReadFile(filepath.Join(dir, activeFileName))
	if rerr != nil {
		if errors.Is(rerr, fs.ErrNotExist) {
			return "", "", ErrNoActive
		}
		return "", "", rerr
	}
	key := strings.TrimSuffix(string(raw), "\n")
	if key == "" {
		return "", "", ErrNoActive
	}
	if !accountKeyPattern.MatchString(key) {
		return key, "", fmt.Errorf("profile: active pointer for %q holds an invalid account key %q", providerKey, key)
	}
	profDir := filepath.Join(dir, key)
	if err := rejectSymlink(profDir); err != nil {
		return key, "", err
	}
	if _, serr := os.Stat(profDir); serr != nil {
		if errors.Is(serr, fs.ErrNotExist) {
			return key, "", ErrActiveMissing
		}
		return key, "", serr
	}
	return key, profDir, nil
}

// lockPath is the per-profile advisory lock file, DELIBERATELY a sibling of
// the profile directory (<providerDir>/<accountKey>.lock) rather than a file
// inside it. Two platform-specific failure modes required this move (found
// by adversarial review against the previous <profileDir>/.amx-profile.lock
// placement):
//
//   - Windows: internal/fslock's TryLock takes a MANDATORY LockFileEx lock on
//     the open handle (fslock.go doc: "a MANDATORY byte-range lock"). If the
//     lock file lived inside the profile directory, Remove's os.RemoveAll
//     would try to delete that very open, locked file as part of removing
//     the tree and fail with a sharing violation — Remove could never
//     succeed while correctly holding its own lock.
//   - Unix: flock() locks an open file descriptor (effectively the inode),
//     not a path (fslock.go doc: unix flock is advisory per-fd). If the lock
//     file lived inside the profile directory, os.RemoveAll would unlink it
//     out from under the still-held lock; a second TryLock on the SAME PATH
//     would then os.OpenFile(O_CREATE) a brand-new file with a fresh inode
//     and lock THAT successfully — even though the first lock's fd is still
//     open on the old, now-unlinked inode. The two locks would stop
//     protecting the same resource, so a Stage racing a Remove could
//     recreate the profile directory while Remove was still deleting it.
//
// A lock file that Remove never deletes (see Remove's doc) keeps its path,
// and therefore its inode, stable for the whole critical section on both
// platforms, which is what makes a second TryLock on the same path correctly
// contend instead of silently locking something else.
func (s *Store) lockPath(providerDir, accountKey string) string {
	return filepath.Join(providerDir, accountKey+".lock")
}

// lock acquires the per-profile advisory lock for accountKey under
// providerDir, retrying on contention up to lockRetryBound. providerDir MUST
// already exist (fslock.TryLock cannot create a lock file under a missing
// parent); every caller in this file creates it (via the profile dir's
// MkdirAll, which creates providerDir as an ancestor) before calling lock.
//
// Before ever calling fslock.TryLock it rejects the lock path being a
// symlink or any other non-regular entry (N4): fslock.TryLock opens with
// O_CREATE, which follows a symlink and would lock/create a file wherever
// that link points — outside the store root entirely if planted there —
// and a directory (or any other non-regular file) planted at the same path
// would make every future TryLock on this profile fail forever, a permanent
// denial-of-service this package could never clear on its own. A path that
// does not exist yet is fine: TryLock creates it fresh as a plain file.
func (s *Store) lock(providerDir, accountKey string) (*fslock.Lock, error) {
	path := s.lockPath(providerDir, accountKey)
	if err := rejectNonRegularLockFile(path); err != nil {
		return nil, err
	}
	deadline := time.Now().Add(lockRetryBound)
	for {
		l, err := fslock.TryLock(path)
		if err == nil {
			return l, nil
		}
		if !errors.Is(err, fslock.ErrWouldBlock) || time.Now().After(deadline) {
			return nil, fmt.Errorf("profile: lock %s held: %w", path, err)
		}
		time.Sleep(lockRetryInterval)
	}
}

// rejectNonRegularLockFile lstat's path and errors if it exists and is not a
// plain regular file. See lock's doc (N4) for why: a symlink would redirect
// fslock's O_CREATE open outside the store root, and a directory would wedge
// every future lock attempt on this path permanently.
func rejectNonRegularLockFile(path string) error {
	info, err := os.Lstat(path)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil
		}
		return err
	}
	if info.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("profile: lock path %s is a symlink, refusing to use it", path)
	}
	if !info.Mode().IsRegular() {
		return fmt.Errorf("profile: lock path %s is not a regular file, refusing to use it", path)
	}
	return nil
}

// validateTemplateRel rejects a Template.Files entry that is empty, absolute,
// contains a ".." component after filepath.Clean (which would let it escape
// the profile directory it is about to be copied into), or names the
// reserved staged-marker file. Unlike a merely-absent source file (silently
// skipped by copyTemplateFile), any of these shapes is always a caller bug,
// so it is an error, not a skip.
func validateTemplateRel(rel string) error {
	if rel == "" {
		return errors.New("empty path")
	}
	if filepath.IsAbs(rel) {
		return errors.New("absolute path not allowed")
	}
	cleaned := filepath.Clean(rel)
	if cleaned == ".." {
		return errors.New("path escapes the profile directory")
	}
	for _, part := range strings.Split(cleaned, string(filepath.Separator)) {
		if part == ".." {
			return errors.New("path escapes the profile directory")
		}
	}
	if filepath.Base(cleaned) == stagedMarkerName {
		return errors.New("reserved file name")
	}
	return nil
}

// copyTemplateFile copies <srcDir>/<rel> to <dstDir>/<rel> unless the source
// is absent (silently skipped — a template need not carry every file a
// caller optimistically lists) or the destination already exists (never
// clobber a file the profile may already have, staged or account-modified).
// The caller (Create) validates rel via validateTemplateRel before this is
// reached, so a ".." escape in rel itself is not this function's concern —
// but rel naming an ordinary-looking subdirectory that is ITSELF a symlink
// (e.g. a pre-planted "hooks -> /elsewhere" inside the profile, with
// rel = "hooks/pre.sh") is a different escape validateTemplateRel cannot see
// (it only inspects the string, never the filesystem), so this function
// rejects that (N5) via rejectSymlinkedAncestors before ever creating or
// writing into dst's parent chain.
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
	if err := rejectSymlinkedAncestors(dstDir, filepath.Dir(rel)); err != nil {
		return err
	}
	dst := filepath.Join(dstDir, rel)
	if err := rejectSymlink(dst); err != nil {
		return err
	}
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

// rejectSymlinkedAncestors walks base/parts[0], base/parts[0]/parts[1], ...
// for each segment of relDir and rejects if any segment that already exists
// is a symlink. A segment that does not exist yet is fine — the caller's
// subsequent os.MkdirAll creates only plain directories for missing
// segments and is a no-op on ones that already exist, so it can never be
// what introduces a symlink itself.
func rejectSymlinkedAncestors(base, relDir string) error {
	if relDir == "" || relDir == "." {
		return nil
	}
	cur := base
	for _, part := range strings.Split(relDir, string(filepath.Separator)) {
		cur = filepath.Join(cur, part)
		if err := rejectSymlink(cur); err != nil {
			return err
		}
	}
	return nil
}

// wipe zeroes b in place, best-effort. It is a courtesy on this ONE []byte
// allocation, not a guarantee that every copy of what it held is gone: any
// code that already parsed b into a Go string (json.Unmarshal into a struct
// field, for instance) holds a separate, immutable allocation this cannot
// reach, because Go strings cannot be wiped once created. Mirrors
// internal/store's wipe() convention (same caveat there, not a stronger
// promise introduced by this package).
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
