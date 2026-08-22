package profile

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/2kwanghee/AMX/ama-agent/internal/fslock"
	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
	"github.com/2kwanghee/AMX/ama-agent/internal/provider/claude"
)

func openStore(t *testing.T) *Store {
	t.Helper()
	s, err := Open(t.TempDir())
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	return s
}

// --- AccountKey -------------------------------------------------------

func TestAccountKeyDeterministic(t *testing.T) {
	a := AccountKey("user@example.com")
	b := AccountKey("user@example.com")
	if a != b {
		t.Fatalf("AccountKey not deterministic: %q vs %q", a, b)
	}
}

func TestAccountKeyCaseInsensitive(t *testing.T) {
	a := AccountKey("User@Example.com")
	b := AccountKey("user@example.com")
	if a != b {
		t.Fatalf("AccountKey should merge case variants of the same email: %q vs %q", a, b)
	}
}

func TestAccountKeyNoCollisionAcrossVariants(t *testing.T) {
	emails := []string{
		"a@b.com",
		"A@B.COM",              // case variant of the above -> same key, checked separately
		"a+tag@b.com",          // plus-addressing, different local part
		"a@b.com ",             // trailing space, TrimSpace should equalize this with a@b.com
		"weird!#$%^&*()@b.com", // special characters
		strings.Repeat("x", 300) + "@example.com", // very long email
		"unicode-이메일@example.com",
	}
	keys := make(map[string]string)
	for _, e := range emails {
		k := AccountKey(e)
		// Every key must be a filesystem-safe, fixed-length hex string.
		if len(k) != 64 {
			t.Fatalf("AccountKey(%q) length = %d, want 64", e, len(k))
		}
		for _, r := range k {
			if !strings.ContainsRune("0123456789abcdef", r) {
				t.Fatalf("AccountKey(%q) = %q contains non-hex rune %q", e, k, r)
			}
		}
		if prev, ok := keys[k]; ok && prev != strings.ToLower(strings.TrimSpace(e)) {
			// Two distinct normalized emails must not collide.
			if strings.ToLower(strings.TrimSpace(e)) != prev {
				t.Fatalf("collision: %q and stored %q both derive key %q", e, prev, k)
			}
		}
		keys[k] = strings.ToLower(strings.TrimSpace(e))
	}
	// "a@b.com " (trimmed) must equal "a@b.com"'s key, and both must differ
	// from "a+tag@b.com"'s.
	if AccountKey("a@b.com") != AccountKey("a@b.com ") {
		t.Fatalf("AccountKey should trim whitespace before hashing")
	}
	if AccountKey("a@b.com") == AccountKey("a+tag@b.com") {
		t.Fatalf("distinct local parts must not collide")
	}
}

// --- Create -------------------------------------------------------------

func TestCreateIdempotent(t *testing.T) {
	s := openStore(t)
	dir1, err := s.Create("claude", "acct1", Template{})
	if err != nil {
		t.Fatalf("first Create: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir1, "marker.txt"), []byte("keep"), 0o600); err != nil {
		t.Fatalf("seed marker: %v", err)
	}
	dir2, err := s.Create("claude", "acct1", Template{})
	if err != nil {
		t.Fatalf("second Create: %v", err)
	}
	if dir1 != dir2 {
		t.Fatalf("Create path changed across calls: %q vs %q", dir1, dir2)
	}
	b, err := os.ReadFile(filepath.Join(dir2, "marker.txt"))
	if err != nil || string(b) != "keep" {
		t.Fatalf("second Create clobbered existing file: err=%v content=%q", err, b)
	}
	info, err := os.Stat(dir2)
	if err != nil {
		t.Fatalf("stat profile dir: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0o700 {
		t.Fatalf("profile dir perm = %o, want 0700", perm)
	}
}

func TestCreateTemplateCopiesExistingSkipsMissing(t *testing.T) {
	s := openStore(t)
	tmplDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(tmplDir, "settings.json"), []byte(`{"common":true}`), 0o644); err != nil {
		t.Fatalf("seed template file: %v", err)
	}
	dir, err := s.Create("claude", "acct1", Template{
		Dir:   tmplDir,
		Files: []string{"settings.json", "does-not-exist.json"},
	})
	if err != nil {
		t.Fatalf("Create with template: %v", err)
	}
	b, err := os.ReadFile(filepath.Join(dir, "settings.json"))
	if err != nil {
		t.Fatalf("template file not copied: %v", err)
	}
	if string(b) != `{"common":true}` {
		t.Fatalf("copied template content = %q", b)
	}
	if _, err := os.Stat(filepath.Join(dir, "does-not-exist.json")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("missing template file should be silently skipped, got err=%v", err)
	}
	info, err := os.Stat(filepath.Join(dir, "settings.json"))
	if err != nil {
		t.Fatalf("stat copied file: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Fatalf("copied template file perm = %o, want 0600", perm)
	}
}

func TestCreateTemplateDoesNotClobberExisting(t *testing.T) {
	s := openStore(t)
	tmplDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(tmplDir, "settings.json"), []byte(`{"common":true}`), 0o644); err != nil {
		t.Fatalf("seed template file: %v", err)
	}
	dir, err := s.Create("claude", "acct1", Template{Dir: tmplDir, Files: []string{"settings.json"}})
	if err != nil {
		t.Fatalf("first Create: %v", err)
	}
	// Simulate the account modifying its own settings after provisioning.
	if err := os.WriteFile(filepath.Join(dir, "settings.json"), []byte(`{"modified":true}`), 0o600); err != nil {
		t.Fatalf("modify settings: %v", err)
	}
	if _, err := s.Create("claude", "acct1", Template{Dir: tmplDir, Files: []string{"settings.json"}}); err != nil {
		t.Fatalf("second Create: %v", err)
	}
	b, err := os.ReadFile(filepath.Join(dir, "settings.json"))
	if err != nil || string(b) != `{"modified":true}` {
		t.Fatalf("re-Create clobbered account-modified template file: err=%v content=%q", err, b)
	}
}

func TestCreateEmptyAccountKeyRejected(t *testing.T) {
	s := openStore(t)
	if _, err := s.Create("claude", "", Template{}); err == nil {
		t.Fatal("Create with empty accountKey should error")
	}
}

// --- Remove --------------------------------------------------------------

func TestRemoveIdempotent(t *testing.T) {
	s := openStore(t)
	if _, err := s.Create("claude", "acct1", Template{}); err != nil {
		t.Fatalf("Create: %v", err)
	}
	if err := s.Remove("claude", "acct1"); err != nil {
		t.Fatalf("first Remove: %v", err)
	}
	if err := s.Remove("claude", "acct1"); err != nil {
		t.Fatalf("second Remove (absent) should be idempotent, got %v", err)
	}
	if err := s.Remove("claude", "never-existed"); err != nil {
		t.Fatalf("Remove of a never-created profile should be idempotent, got %v", err)
	}
}

// --- Isolation -------------------------------------------------------------

func TestIsolationBetweenAccounts(t *testing.T) {
	s := openStore(t)
	dir1, err := s.Create("claude", "acct1", Template{})
	if err != nil {
		t.Fatalf("Create acct1: %v", err)
	}
	dir2, err := s.Create("claude", "acct2", Template{})
	if err != nil {
		t.Fatalf("Create acct2: %v", err)
	}
	if dir1 == dir2 {
		t.Fatalf("two accounts resolved to the same profile dir: %q", dir1)
	}
	if err := os.WriteFile(filepath.Join(dir1, "only-in-acct1.txt"), []byte("x"), 0o600); err != nil {
		t.Fatalf("write into acct1: %v", err)
	}
	if err := s.Remove("claude", "acct1"); err != nil {
		t.Fatalf("Remove acct1: %v", err)
	}
	if _, err := os.Stat(dir2); err != nil {
		t.Fatalf("removing acct1 affected acct2: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir2, "only-in-acct1.txt")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("acct1's file leaked into acct2's directory")
	}
}

func TestIsolationAcrossProviders(t *testing.T) {
	s := openStore(t)
	key := AccountKey("same@example.com")
	dirClaude, err := s.Create("claude", key, Template{})
	if err != nil {
		t.Fatalf("Create claude: %v", err)
	}
	dirOther, err := s.Create("other-provider", key, Template{})
	if err != nil {
		t.Fatalf("Create other-provider: %v", err)
	}
	if dirClaude == dirOther {
		t.Fatalf("same accountKey under two providers collided: %q", dirClaude)
	}
}

// --- Stage / Fingerprint --------------------------------------------------

func sampleCredential(refreshToken string) []byte {
	b, _ := json.Marshal(map[string]any{
		"claudeAiOauth": map[string]string{
			"accessToken":  "at-value",
			"refreshToken": refreshToken,
		},
	})
	return b
}

func TestStageWritesCredentialWithPermissions(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	cred := sampleCredential("rt-123")
	meta := provider.AddMeta{Email: "user@example.com", OrganizationName: "Acme"}
	key := AccountKey(meta.Email)

	if err := s.Stage(drv, key, cred, meta); err != nil {
		t.Fatalf("Stage: %v", err)
	}

	credPath := drv.CredentialPath(s.ProfileDir(drv.Name(), key))
	info, err := os.Stat(credPath)
	if err != nil {
		t.Fatalf("credential file not created: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Fatalf("credential file perm = %o, want 0600", perm)
	}
	dirInfo, err := os.Stat(s.ProfileDir(drv.Name(), key))
	if err != nil {
		t.Fatalf("stat profile dir: %v", err)
	}
	if perm := dirInfo.Mode().Perm(); perm != 0o700 {
		t.Fatalf("profile dir perm = %o, want 0700", perm)
	}
}

func TestFingerprintMatchesDriverRule(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	cred := sampleCredential("rt-456")
	meta := provider.AddMeta{Email: "fp@example.com"}
	key := AccountKey(meta.Email)

	if err := s.Stage(drv, key, cred, meta); err != nil {
		t.Fatalf("Stage: %v", err)
	}
	got, err := s.Fingerprint(drv, key)
	if err != nil {
		t.Fatalf("Fingerprint: %v", err)
	}
	want := drv.Fingerprint(cred)
	if got != want {
		t.Fatalf("Store.Fingerprint = %q, want driver rule %q", got, want)
	}
	if !strings.HasPrefix(got, "sha256:") {
		t.Fatalf("expected refreshToken-based fingerprint, got %q", got)
	}
}

func TestStageEmptyAccountKeyRejected(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	err := s.Stage(drv, "", sampleCredential("rt"), provider.AddMeta{})
	if err == nil {
		t.Fatal("Stage with empty accountKey should error")
	}
}

// --- Active pointer --------------------------------------------------------

func TestGetActiveNoneSet(t *testing.T) {
	s := openStore(t)
	_, _, err := s.GetActive("claude")
	if !errors.Is(err, ErrNoActive) {
		t.Fatalf("GetActive with no pointer written: err = %v, want ErrNoActive", err)
	}
}

func TestSetActiveThenGetActive(t *testing.T) {
	s := openStore(t)
	dir, err := s.Create("claude", "acct1", Template{})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	if err := s.SetActive("claude", "acct1"); err != nil {
		t.Fatalf("SetActive: %v", err)
	}
	key, got, err := s.GetActive("claude")
	if err != nil {
		t.Fatalf("GetActive: %v", err)
	}
	if key != "acct1" || got != dir {
		t.Fatalf("GetActive = (%q, %q), want (%q, %q)", key, got, "acct1", dir)
	}
}

func TestSetActiveIsAtomic(t *testing.T) {
	s := openStore(t)
	if _, err := s.Create("claude", "acct1", Template{}); err != nil {
		t.Fatalf("Create acct1: %v", err)
	}
	if _, err := s.Create("claude", "acct2", Template{}); err != nil {
		t.Fatalf("Create acct2: %v", err)
	}
	if err := s.SetActive("claude", "acct1"); err != nil {
		t.Fatalf("SetActive acct1: %v", err)
	}
	pointerPath := filepath.Join(s.providerDir("claude"), activeFileName)
	before, err := os.Stat(pointerPath)
	if err != nil {
		t.Fatalf("stat pointer: %v", err)
	}
	if err := s.SetActive("claude", "acct2"); err != nil {
		t.Fatalf("SetActive acct2: %v", err)
	}
	// The pointer file identity (inode) changes on every write because
	// SetActive writes via temp+rename, never truncates-in-place; that is the
	// atomicity guarantee this test protects.
	after, err := os.Stat(pointerPath)
	if err != nil {
		t.Fatalf("stat pointer after: %v", err)
	}
	if os.SameFile(before, after) {
		t.Fatalf("pointer file was modified in place, not replaced atomically via rename")
	}
	key, _, err := s.GetActive("claude")
	if err != nil || key != "acct2" {
		t.Fatalf("GetActive after second SetActive = (%q, %v), want acct2", key, err)
	}
}

func TestGetActivePointsToMissingProfile(t *testing.T) {
	s := openStore(t)
	if _, err := s.Create("claude", "acct1", Template{}); err != nil {
		t.Fatalf("Create: %v", err)
	}
	if err := s.SetActive("claude", "acct1"); err != nil {
		t.Fatalf("SetActive: %v", err)
	}
	if err := s.Remove("claude", "acct1"); err != nil {
		t.Fatalf("Remove: %v", err)
	}
	key, dir, err := s.GetActive("claude")
	if !errors.Is(err, ErrActiveMissing) {
		t.Fatalf("GetActive after removing the active profile: err = %v, want ErrActiveMissing", err)
	}
	if key != "acct1" {
		t.Fatalf("GetActive should still report the missing key, got %q", key)
	}
	if dir != "" {
		t.Fatalf("GetActive should return an empty dir on ErrActiveMissing, got %q", dir)
	}
}

// --- List --------------------------------------------------------------

func TestListEmptyProviderNoError(t *testing.T) {
	s := openStore(t)
	keys, err := s.List("claude")
	if err != nil {
		t.Fatalf("List on untouched provider: %v", err)
	}
	if len(keys) != 0 {
		t.Fatalf("List = %v, want empty", keys)
	}
}

func TestListSortedAndExcludesPointer(t *testing.T) {
	s := openStore(t)
	for _, k := range []string{"b-acct", "a-acct"} {
		if _, err := s.Create("claude", k, Template{}); err != nil {
			t.Fatalf("Create %s: %v", k, err)
		}
	}
	if err := s.SetActive("claude", "a-acct"); err != nil {
		t.Fatalf("SetActive: %v", err)
	}
	keys, err := s.List("claude")
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(keys) != 2 || keys[0] != "a-acct" || keys[1] != "b-acct" {
		t.Fatalf("List = %v, want sorted [a-acct b-acct] (activeFileName excluded)", keys)
	}
}

// --- Lock --------------------------------------------------------------

func TestStageBlockedByHeldLockReturnsError(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	key := AccountKey("locked@example.com")
	dir, err := s.Create(drv.Name(), key, Template{})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	held, err := fslock.TryLock(filepath.Join(dir, lockFileName))
	if err != nil {
		t.Fatalf("TryLock: %v", err)
	}
	defer held.Unlock()

	err = s.Stage(drv, key, sampleCredential("rt"), provider.AddMeta{Email: "locked@example.com"})
	if err == nil {
		t.Fatal("Stage should fail while the profile lock is held elsewhere")
	}
}

func TestRemoveBlockedByHeldLockReturnsError(t *testing.T) {
	s := openStore(t)
	dir, err := s.Create("claude", "acct1", Template{})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	held, err := fslock.TryLock(filepath.Join(dir, lockFileName))
	if err != nil {
		t.Fatalf("TryLock: %v", err)
	}
	defer held.Unlock()

	if err := s.Remove("claude", "acct1"); err == nil {
		t.Fatal("Remove should fail while the profile lock is held elsewhere")
	}
}

// --- Env / Path --------------------------------------------------------

func TestEnvDelegatesToDriver(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	key := "acct1"
	dir, err := s.Create(drv.Name(), key, Template{})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	env := s.Env(drv, key)
	want := drv.Env(dir)
	if len(env) != len(want) || (len(env) > 0 && env[0] != want[0]) {
		t.Fatalf("Env = %v, want %v", env, want)
	}
}
