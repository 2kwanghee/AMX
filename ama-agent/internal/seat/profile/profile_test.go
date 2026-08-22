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

func mustProfileDir(t *testing.T, s *Store, providerKey, accountKey string) string {
	t.Helper()
	dir, err := s.ProfileDir(providerKey, accountKey)
	if err != nil {
		t.Fatalf("ProfileDir(%q, %q): %v", providerKey, accountKey, err)
	}
	return dir
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

// TestAccountKeyNoCollisionAcrossVariants derives keys for a set of inputs
// where every entry EXCEPT "a@b.com " is a genuinely distinct account, and
// fails if any two distinct normalized emails ever land on the same key. A
// prior version of this test compared each key only against itself and could
// never fail regardless of what AccountKey did; this version actually
// exercises the no-collision claim.
func TestAccountKeyNoCollisionAcrossVariants(t *testing.T) {
	inputs := []string{
		"a@b.com",
		"a@b.com ", // trailing space: same normalized email as above, on purpose
		"a+tag@b.com",
		"weird!#$%^&*()@b.com",
		strings.Repeat("x", 300) + "@example.com",
		"unicode-이메일@example.com",
	}
	seenBy := make(map[string]string) // key -> normalized email that first produced it
	for _, e := range inputs {
		k := AccountKey(e)
		if !accountKeyPattern.MatchString(k) {
			t.Fatalf("AccountKey(%q) = %q is not a valid 64-hex key", e, k)
		}
		norm := strings.ToLower(strings.TrimSpace(e))
		if prevNorm, ok := seenBy[k]; ok {
			if prevNorm != norm {
				t.Fatalf("collision: %q and %q both derive key %q", prevNorm, norm, k)
			}
			continue // expected: same normalized email as a prior entry
		}
		seenBy[k] = norm
	}
	if AccountKey("a@b.com") == AccountKey("a+tag@b.com") {
		t.Fatalf("distinct local parts must not collide")
	}
}

// --- Create -------------------------------------------------------------

func TestCreateIdempotent(t *testing.T) {
	s := openStore(t)
	key := AccountKey("acct1@example.com")
	dir1, err := s.Create("claude", key, Template{})
	if err != nil {
		t.Fatalf("first Create: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir1, "marker.txt"), []byte("keep"), 0o600); err != nil {
		t.Fatalf("seed marker: %v", err)
	}
	dir2, err := s.Create("claude", key, Template{})
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
	key := AccountKey("acct1@example.com")
	dir, err := s.Create("claude", key, Template{
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
	key := AccountKey("acct1@example.com")
	dir, err := s.Create("claude", key, Template{Dir: tmplDir, Files: []string{"settings.json"}})
	if err != nil {
		t.Fatalf("first Create: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "settings.json"), []byte(`{"modified":true}`), 0o600); err != nil {
		t.Fatalf("modify settings: %v", err)
	}
	if _, err := s.Create("claude", key, Template{Dir: tmplDir, Files: []string{"settings.json"}}); err != nil {
		t.Fatalf("second Create: %v", err)
	}
	b, err := os.ReadFile(filepath.Join(dir, "settings.json"))
	if err != nil || string(b) != `{"modified":true}` {
		t.Fatalf("re-Create clobbered account-modified template file: err=%v content=%q", err, b)
	}
}

func TestCreateTemplateRelEscapeRejected(t *testing.T) {
	s := openStore(t)
	tmplDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(tmplDir, "payload.json"), []byte("x"), 0o644); err != nil {
		t.Fatalf("seed payload: %v", err)
	}
	key := AccountKey("escape@example.com")
	_, err := s.Create("claude", key, Template{Dir: tmplDir, Files: []string{"../payload.json"}})
	if err == nil {
		t.Fatal("Create should reject a Template.Files entry that escapes the profile directory")
	}
	// Must not have landed anywhere outside the profile, in particular not
	// as a sibling of the store root.
	if _, statErr := os.Stat(filepath.Join(filepath.Dir(s.root), "payload.json")); !errors.Is(statErr, os.ErrNotExist) {
		t.Fatalf("escaped template file was written outside the store: %v", statErr)
	}
}

// TestCreateTemplateRejectsSymlinkedSubdirectory is N5's exact reproduction:
// validateTemplateRel only inspects the rel STRING (no ".." component), so a
// rel like "hooks/pre.sh" sails through it even when "hooks" is itself a
// pre-planted symlink pointing outside the profile — the escape happens at
// the filesystem level, which is what rejectSymlinkedAncestors must catch.
func TestCreateTemplateRejectsSymlinkedSubdirectory(t *testing.T) {
	if runtimeIsWindows() {
		t.Skip("symlink creation semantics differ on windows; covered by unix CI")
	}
	s := openStore(t)
	tmplDir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(tmplDir, "hooks"), 0o755); err != nil {
		t.Fatalf("mkdir template hooks dir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(tmplDir, "hooks", "pre.sh"), []byte("payload"), 0o644); err != nil {
		t.Fatalf("seed template hook file: %v", err)
	}

	key := AccountKey("hookescape@example.com")
	dir, err := s.Create("claude", key, Template{}) // bare profile, no template yet
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	elsewhere := filepath.Join(t.TempDir(), "elsewhere")
	if err := os.MkdirAll(elsewhere, 0o700); err != nil {
		t.Fatalf("mkdir elsewhere: %v", err)
	}
	if err := os.Symlink(elsewhere, filepath.Join(dir, "hooks")); err != nil {
		t.Fatalf("symlink hooks subdirectory: %v", err)
	}

	_, err = s.Create("claude", key, Template{Dir: tmplDir, Files: []string{"hooks/pre.sh"}})
	if err == nil {
		t.Fatal("Create should reject a template file whose destination subdirectory is a symlink")
	}
	if _, statErr := os.Stat(filepath.Join(elsewhere, "pre.sh")); !errors.Is(statErr, os.ErrNotExist) {
		t.Fatalf("payload escaped through the symlinked hooks subdirectory: %v", statErr)
	}
}

// --- Path validation (adversarial) --------------------------------------

func TestProviderKeyPathEscapeRejected(t *testing.T) {
	s := openStore(t)
	for _, bad := range []string{".", "..", "../..", "a/b", "/etc", "Claude", ""} {
		if bad == "" {
			continue // empty providerKey normalizes to "claude" and is valid
		}
		if _, err := s.Create(bad, AccountKey("x@example.com"), Template{}); err == nil {
			t.Fatalf("Create with providerKey %q should be rejected", bad)
		}
	}
}

func TestAccountKeyPathEscapeRejected(t *testing.T) {
	s := openStore(t)
	for _, bad := range []string{".", "..", "../..", "../../etc/passwd", "not-hex", strings.Repeat("a", 63), strings.Repeat("a", 65)} {
		if err := s.Remove("claude", bad); err == nil {
			t.Fatalf("Remove with accountKey %q should be rejected, not silently accepted", bad)
		}
		if _, err := s.Create("claude", bad, Template{}); err == nil {
			t.Fatalf("Create with accountKey %q should be rejected", bad)
		}
	}
}

// TestRemoveRejectsTraversalInsteadOfWipingRoot is the adversarial review's
// exact reproduction: Remove("claude", ".") / ("..") / ("../..") must error
// out, not delete the profiles root, the provider directory, or anything
// alongside stateDir (manifest.enc's directory).
func TestRemoveRejectsTraversalInsteadOfWipingRoot(t *testing.T) {
	stateDir := t.TempDir()
	s, err := Open(stateDir)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	key := AccountKey("survivor@example.com")
	if _, err := s.Create("claude", key, Template{}); err != nil {
		t.Fatalf("Create survivor profile: %v", err)
	}
	sentinel := filepath.Join(stateDir, "manifest.enc")
	if err := os.WriteFile(sentinel, []byte("do-not-touch"), 0o600); err != nil {
		t.Fatalf("seed sentinel: %v", err)
	}

	for _, bad := range []string{".", "..", "../.."} {
		if err := s.Remove("claude", bad); err == nil {
			t.Fatalf("Remove(\"claude\", %q) should error, not delete anything", bad)
		}
	}

	if _, err := os.Stat(sentinel); err != nil {
		t.Fatalf("sentinel outside the profile store was affected: %v", err)
	}
	dir := mustProfileDir(t, s, "claude", key)
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("unrelated survivor profile was affected: %v", err)
	}
}

func TestSetActivePathEscapeRejected(t *testing.T) {
	s := openStore(t)
	if err := s.SetActive("../../..", "hijack"); err == nil {
		t.Fatal("SetActive with a path-escaping providerKey/accountKey should be rejected")
	}
	if err := s.SetActive("claude", "hijack"); err == nil {
		t.Fatal("SetActive with a non-hex accountKey should be rejected")
	}
}

// TestGetActiveRejectsTamperedPointerContent writes an out-of-shape value
// directly into the active pointer file (bypassing SetActive, as a corrupted
// or hand-edited file would) and checks GetActive refuses to turn it into a
// path instead of silently joining it.
func TestGetActiveRejectsTamperedPointerContent(t *testing.T) {
	s := openStore(t)
	providerDir, err := s.resolveProviderDir("claude")
	if err != nil {
		t.Fatalf("resolveProviderDir: %v", err)
	}
	if err := os.MkdirAll(providerDir, 0o700); err != nil {
		t.Fatalf("mkdir provider dir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(providerDir, activeFileName), []byte("../../../etc"), 0o600); err != nil {
		t.Fatalf("write tampered pointer: %v", err)
	}
	key, dir, err := s.GetActive("claude")
	if err == nil {
		t.Fatalf("GetActive should reject a non-hex pointer content, got dir=%q", dir)
	}
	if errors.Is(err, ErrActiveMissing) || errors.Is(err, ErrNoActive) {
		t.Fatalf("tampered pointer content should be its own error kind, got %v", err)
	}
	if dir != "" {
		t.Fatalf("GetActive must not return a path for invalid pointer content, got %q", dir)
	}
	_ = key
}

// TestGetActiveRejectsLeadingWhitespace is the adversarial-review F2
// reproduction: a leading space (or any other stray byte) in the pointer
// content must be REJECTED, not silently trimmed the way strings.TrimSpace
// used to. Before this fix, this exact content made deploy/amx-claude reject
// the pointer (its case pattern rejects any non-hex byte anywhere) while the
// old TrimSpace-based GetActive/session_usage_hook.py's .strip() accepted
// it — the three-reader disagreement F2 reproduced end to end.
func TestGetActiveRejectsLeadingWhitespace(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	key := AccountKey("leading-space@example.com")
	if err := s.Stage(drv, key, sampleCredential("rt-lw"), provider.AddMeta{Email: "leading-space@example.com"}); err != nil {
		t.Fatalf("Stage: %v", err)
	}
	providerDir, err := s.resolveProviderDir("claude")
	if err != nil {
		t.Fatalf("resolveProviderDir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(providerDir, activeFileName), []byte(" "+key), 0o600); err != nil {
		t.Fatalf("write pointer with leading whitespace: %v", err)
	}
	_, dir, err := s.GetActive("claude")
	if err == nil {
		t.Fatalf("GetActive must reject a pointer with leading whitespace, got dir=%q", dir)
	}
	if errors.Is(err, ErrActiveMissing) || errors.Is(err, ErrNoActive) {
		t.Fatalf("a leading-whitespace pointer must be its own error kind (invalid format), got %v", err)
	}
}

// TestGetActiveAcceptsExactlyOneTrailingNewline confirms the ONE allowed
// deviation from a bare 64-hex-char pointer: precisely one trailing "\n".
func TestGetActiveAcceptsExactlyOneTrailingNewline(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	key := AccountKey("trailing-newline@example.com")
	if err := s.Stage(drv, key, sampleCredential("rt-tn"), provider.AddMeta{Email: "trailing-newline@example.com"}); err != nil {
		t.Fatalf("Stage: %v", err)
	}
	providerDir, err := s.resolveProviderDir("claude")
	if err != nil {
		t.Fatalf("resolveProviderDir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(providerDir, activeFileName), []byte(key+"\n"), 0o600); err != nil {
		t.Fatalf("write pointer with one trailing newline: %v", err)
	}
	got, dir, err := s.GetActive("claude")
	if err != nil {
		t.Fatalf("GetActive with one trailing newline: %v", err)
	}
	if got != key || dir == "" {
		t.Fatalf("GetActive = (%q, %q), want (%q, non-empty)", got, dir, key)
	}
}

// TestGetActiveRejectsTwoTrailingNewlines: TrimSuffix removes at most ONE
// trailing "\n" by design — a second one left behind must fail the exact
// 64-hex-char match, not be silently swallowed the way TrimSpace would.
func TestGetActiveRejectsTwoTrailingNewlines(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	key := AccountKey("double-newline@example.com")
	if err := s.Stage(drv, key, sampleCredential("rt-dn"), provider.AddMeta{Email: "double-newline@example.com"}); err != nil {
		t.Fatalf("Stage: %v", err)
	}
	providerDir, err := s.resolveProviderDir("claude")
	if err != nil {
		t.Fatalf("resolveProviderDir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(providerDir, activeFileName), []byte(key+"\n\n"), 0o600); err != nil {
		t.Fatalf("write pointer with two trailing newlines: %v", err)
	}
	_, _, err = s.GetActive("claude")
	if err == nil {
		t.Fatal("GetActive must reject a pointer with two trailing newlines")
	}
	if errors.Is(err, ErrActiveMissing) || errors.Is(err, ErrNoActive) {
		t.Fatalf("a double-trailing-newline pointer must be its own error kind, got %v", err)
	}
}

// --- Pre-existing permissive directory / symlink -------------------------

func TestCreateTightensPreExistingWideDirectory(t *testing.T) {
	s := openStore(t)
	key := AccountKey("wide@example.com")
	dir, err := s.ProfileDir("claude", key)
	if err != nil {
		t.Fatalf("ProfileDir: %v", err)
	}
	if err := os.MkdirAll(dir, 0o777); err != nil {
		t.Fatalf("pre-create wide dir: %v", err)
	}
	if err := os.Chmod(dir, 0o777); err != nil {
		t.Fatalf("chmod wide: %v", err)
	}
	providerDir := filepath.Dir(dir)
	if err := os.Chmod(providerDir, 0o777); err != nil {
		t.Fatalf("chmod provider wide: %v", err)
	}

	if _, err := s.Create("claude", key, Template{}); err != nil {
		t.Fatalf("Create over a pre-existing wide directory: %v", err)
	}

	info, err := os.Stat(dir)
	if err != nil {
		t.Fatalf("stat profile dir: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0o700 {
		t.Fatalf("profile dir perm after Create = %o, want tightened to 0700", perm)
	}
	pinfo, err := os.Stat(providerDir)
	if err != nil {
		t.Fatalf("stat provider dir: %v", err)
	}
	if perm := pinfo.Mode().Perm(); perm != 0o700 {
		t.Fatalf("provider dir perm after Create = %o, want tightened to 0700", perm)
	}
}

func TestStageTightensPreExistingWideDirectory(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	key := AccountKey("wide-stage@example.com")
	dir, err := s.ProfileDir(drv.Name(), key)
	if err != nil {
		t.Fatalf("ProfileDir: %v", err)
	}
	if err := os.MkdirAll(dir, 0o777); err != nil {
		t.Fatalf("pre-create wide dir: %v", err)
	}
	if err := os.Chmod(dir, 0o777); err != nil {
		t.Fatalf("chmod wide: %v", err)
	}

	if err := s.Stage(drv, key, sampleCredential("rt-wide"), provider.AddMeta{Email: "wide-stage@example.com"}); err != nil {
		t.Fatalf("Stage: %v", err)
	}
	info, err := os.Stat(dir)
	if err != nil {
		t.Fatalf("stat profile dir: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0o700 {
		t.Fatalf("profile dir perm after Stage = %o, want tightened to 0700", perm)
	}
}

func TestCreateRejectsSymlinkedProfileDir(t *testing.T) {
	if runtimeIsWindows() {
		t.Skip("symlink creation semantics differ on windows; covered by unix CI")
	}
	s := openStore(t)
	key := AccountKey("linked@example.com")
	dir, err := s.ProfileDir("claude", key)
	if err != nil {
		t.Fatalf("ProfileDir: %v", err)
	}
	providerDir := filepath.Dir(dir)
	if err := os.MkdirAll(providerDir, 0o700); err != nil {
		t.Fatalf("mkdir provider dir: %v", err)
	}
	elsewhere := filepath.Join(t.TempDir(), "elsewhere")
	if err := os.MkdirAll(elsewhere, 0o700); err != nil {
		t.Fatalf("mkdir elsewhere: %v", err)
	}
	if err := os.Symlink(elsewhere, dir); err != nil {
		t.Fatalf("symlink profile dir: %v", err)
	}

	if _, err := s.Create("claude", key, Template{}); err == nil {
		t.Fatal("Create over a symlinked profile directory should be rejected")
	}
	if _, err := os.Stat(filepath.Join(elsewhere, "marker-should-not-exist")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("unexpected write through the symlink: %v", err)
	}
}

func TestStageRejectsSymlinkedProviderDir(t *testing.T) {
	if runtimeIsWindows() {
		t.Skip("symlink creation semantics differ on windows; covered by unix CI")
	}
	s := openStore(t)
	drv := claude.New()
	key := AccountKey("linked-provider@example.com")
	providerDir, err := s.resolveProviderDir(drv.Name())
	if err != nil {
		t.Fatalf("resolveProviderDir: %v", err)
	}
	elsewhere := filepath.Join(t.TempDir(), "elsewhere")
	if err := os.MkdirAll(elsewhere, 0o700); err != nil {
		t.Fatalf("mkdir elsewhere: %v", err)
	}
	if err := os.Symlink(elsewhere, providerDir); err != nil {
		t.Fatalf("symlink provider dir: %v", err)
	}

	err = s.Stage(drv, key, sampleCredential("rt"), provider.AddMeta{Email: "linked-provider@example.com"})
	if err == nil {
		t.Fatal("Stage through a symlinked provider directory should be rejected")
	}
	if _, statErr := os.Stat(filepath.Join(elsewhere, key)); !errors.Is(statErr, os.ErrNotExist) {
		t.Fatalf("unexpected write through the symlinked provider dir: %v", statErr)
	}
}

// TestOpenRejectsSymlinkedRoot is N1's exact reproduction: planting
// <stateDir>/profiles as a symlink BEFORE Open is ever called used to defeat
// every other check in this file, since providerDir/profileDir would then be
// real directories at the symlink's target and no per-level rejectSymlink
// call ever looked at root itself.
func TestOpenRejectsSymlinkedRoot(t *testing.T) {
	if runtimeIsWindows() {
		t.Skip("symlink creation semantics differ on windows; covered by unix CI")
	}
	stateDir := t.TempDir()
	elsewhere := filepath.Join(t.TempDir(), "elsewhere")
	if err := os.MkdirAll(elsewhere, 0o700); err != nil {
		t.Fatalf("mkdir elsewhere: %v", err)
	}
	if err := os.Symlink(elsewhere, filepath.Join(stateDir, profilesSubdir)); err != nil {
		t.Fatalf("symlink profiles root: %v", err)
	}

	if _, err := Open(stateDir); err == nil {
		t.Fatal("Open should reject a pre-existing symlinked profiles root")
	}
	if _, statErr := os.Stat(filepath.Join(elsewhere, "claude")); !errors.Is(statErr, os.ErrNotExist) {
		t.Fatalf("Open must not have written through the symlinked root: %v", statErr)
	}
}

// TestOpenTightensPreExistingWideRoot exercises N1's ensureDirPerm(root)
// call: a root directory that already existed at a wider mode before Open
// is tightened to 0700, matching Create/Stage/SetActive's self-heal.
func TestOpenTightensPreExistingWideRoot(t *testing.T) {
	stateDir := t.TempDir()
	root := filepath.Join(stateDir, profilesSubdir)
	if err := os.MkdirAll(root, 0o777); err != nil {
		t.Fatalf("pre-create wide root: %v", err)
	}
	if err := os.Chmod(root, 0o777); err != nil {
		t.Fatalf("chmod wide: %v", err)
	}
	if _, err := Open(stateDir); err != nil {
		t.Fatalf("Open over a pre-existing wide root: %v", err)
	}
	info, err := os.Stat(root)
	if err != nil {
		t.Fatalf("stat root: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0o700 {
		t.Fatalf("root perm after Open = %o, want tightened to 0700", perm)
	}
}

// TestRemoveConfirmWithinRootUsesResolvedPaths is a white-box test of
// confirmWithinRoot itself (N1b): it must reject a path that lexically looks
// like a sibling of root ("/root-evil" vs "/root") rather than a true child,
// proving the boundary check appends the separator instead of doing a bare
// string-prefix match.
func TestRemoveConfirmWithinRootUsesResolvedPaths(t *testing.T) {
	s := openStore(t)
	sibling := s.root + "-evil"
	if err := os.MkdirAll(sibling, 0o700); err != nil {
		t.Fatalf("mkdir sibling: %v", err)
	}
	if err := s.confirmWithinRoot(sibling); err == nil {
		t.Fatalf("confirmWithinRoot must reject a sibling directory (%q) that merely shares root's string prefix", sibling)
	}
	// A genuine descendant must still pass.
	key := AccountKey("within@example.com")
	dir, err := s.Create("claude", key, Template{})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	if err := s.confirmWithinRoot(dir); err != nil {
		t.Fatalf("confirmWithinRoot rejected a genuine descendant: %v", err)
	}
}

// --- Remove --------------------------------------------------------------

func TestRemoveIdempotent(t *testing.T) {
	s := openStore(t)
	key := AccountKey("acct1@example.com")
	if _, err := s.Create("claude", key, Template{}); err != nil {
		t.Fatalf("Create: %v", err)
	}
	if err := s.Remove("claude", key); err != nil {
		t.Fatalf("first Remove: %v", err)
	}
	if err := s.Remove("claude", key); err != nil {
		t.Fatalf("second Remove (absent) should be idempotent, got %v", err)
	}
	if err := s.Remove("claude", AccountKey("never-existed@example.com")); err != nil {
		t.Fatalf("Remove of a never-created profile should be idempotent, got %v", err)
	}
}

// --- Isolation -------------------------------------------------------------

func TestIsolationBetweenAccounts(t *testing.T) {
	s := openStore(t)
	key1 := AccountKey("acct1@example.com")
	key2 := AccountKey("acct2@example.com")
	dir1, err := s.Create("claude", key1, Template{})
	if err != nil {
		t.Fatalf("Create acct1: %v", err)
	}
	dir2, err := s.Create("claude", key2, Template{})
	if err != nil {
		t.Fatalf("Create acct2: %v", err)
	}
	if dir1 == dir2 {
		t.Fatalf("two accounts resolved to the same profile dir: %q", dir1)
	}
	if err := os.WriteFile(filepath.Join(dir1, "only-in-acct1.txt"), []byte("x"), 0o600); err != nil {
		t.Fatalf("write into acct1: %v", err)
	}
	if err := s.Remove("claude", key1); err != nil {
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

// --- Stage / Fingerprint / Complete ---------------------------------------

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

	dir := mustProfileDir(t, s, drv.Name(), key)
	credPath := drv.CredentialPath(dir)
	info, err := os.Stat(credPath)
	if err != nil {
		t.Fatalf("credential file not created: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Fatalf("credential file perm = %o, want 0600", perm)
	}
	dirInfo, err := os.Stat(dir)
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

func TestCompleteReflectsStagedMarker(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	key := AccountKey("complete@example.com")

	if _, err := s.Create(drv.Name(), key, Template{}); err != nil {
		t.Fatalf("Create: %v", err)
	}
	done, err := s.Complete(drv, key)
	if err != nil {
		t.Fatalf("Complete after Create only: %v", err)
	}
	if done {
		t.Fatal("Complete should be false before Stage ever ran")
	}

	if err := s.Stage(drv, key, sampleCredential("rt-complete"), provider.AddMeta{Email: "complete@example.com"}); err != nil {
		t.Fatalf("Stage: %v", err)
	}
	done, err = s.Complete(drv, key)
	if err != nil {
		t.Fatalf("Complete after Stage: %v", err)
	}
	if !done {
		t.Fatal("Complete should be true after a successful Stage")
	}
}

func TestCompleteFalseWhenMarkerMissingDespiteCredentialFile(t *testing.T) {
	// Simulates a Stage that died after StageCredential wrote the credential
	// file but before the marker write (process kill, disk full): the
	// profile "exists" and even has a usable-looking credential file, but
	// Complete must still say false.
	s := openStore(t)
	drv := claude.New()
	key := AccountKey("partial@example.com")
	dir, err := s.Create(drv.Name(), key, Template{})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	if err := drv.StageCredential(dir, sampleCredential("rt-partial"), provider.AddMeta{Email: "partial@example.com"}); err != nil {
		t.Fatalf("direct StageCredential (simulating partial Stage): %v", err)
	}
	if _, err := os.Stat(drv.CredentialPath(dir)); err != nil {
		t.Fatalf("credential file should exist for this scenario: %v", err)
	}
	done, err := s.Complete(drv, key)
	if err != nil {
		t.Fatalf("Complete: %v", err)
	}
	if done {
		t.Fatal("Complete must be false when the marker was never written, even if the credential file exists")
	}
}

// TestCompleteFalseWhenMarkerPresentButCredentialMissing is N3's exact
// reproduction: a marker planted directly on disk (bypassing Stage) with no
// credential file behind it at all must not read as Complete.
func TestCompleteFalseWhenMarkerPresentButCredentialMissing(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	key := AccountKey("premarked@example.com")
	dir, err := s.Create(drv.Name(), key, Template{})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, stagedMarkerName), []byte("sha256:deadbeef"), 0o600); err != nil {
		t.Fatalf("plant marker: %v", err)
	}
	done, err := s.Complete(drv, key)
	if err != nil {
		t.Fatalf("Complete: %v", err)
	}
	if done {
		t.Fatal("Complete must be false when no credential file exists, regardless of a pre-planted marker")
	}
}

// TestCompleteFalseWhenMarkerFingerprintMismatchesLiveCredential simulates an
// external rotation (something other than Store.Stage rewriting the
// credential file, e.g. Claude Code's own refresh, or a directly-forged
// file) that leaves the OLD marker in place next to a NEW credential whose
// fingerprint no longer matches it.
func TestCompleteFalseWhenMarkerFingerprintMismatchesLiveCredential(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	key := AccountKey("mismatch@example.com")
	if err := s.Stage(drv, key, sampleCredential("rt-a"), provider.AddMeta{Email: "mismatch@example.com"}); err != nil {
		t.Fatalf("Stage: %v", err)
	}
	dir, _, err := s.resolveProfile(drv.Name(), key)
	if err != nil {
		t.Fatalf("resolveProfile: %v", err)
	}
	if err := drv.StageCredential(dir, sampleCredential("rt-b"), provider.AddMeta{Email: "mismatch@example.com"}); err != nil {
		t.Fatalf("external StageCredential (simulating a rotation Stage never saw): %v", err)
	}
	done, err := s.Complete(drv, key)
	if err != nil {
		t.Fatalf("Complete: %v", err)
	}
	if done {
		t.Fatal("Complete should be false once the marker's fingerprint no longer matches the live credential")
	}
}

// TestReStageDyingMidwayLeavesCompleteFalse is N2's exact reproduction: Stage
// succeeds once (Complete==true), then a re-Stage is modeled as dying right
// after the marker-invalidation step and StageCredential's write, but before
// the new marker is ever recorded — which is precisely what Stage's own
// ordering (delete marker, then StageCredential, then write new marker)
// produces if the process is killed in that window.
func TestReStageDyingMidwayLeavesCompleteFalse(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	key := AccountKey("rotate@example.com")
	if err := s.Stage(drv, key, sampleCredential("rt-1"), provider.AddMeta{Email: "rotate@example.com"}); err != nil {
		t.Fatalf("first Stage: %v", err)
	}
	done, err := s.Complete(drv, key)
	if err != nil || !done {
		t.Fatalf("Complete after first Stage = (%v, %v), want (true, nil)", done, err)
	}

	dir, _, err := s.resolveProfile(drv.Name(), key)
	if err != nil {
		t.Fatalf("resolveProfile: %v", err)
	}
	if err := os.Remove(filepath.Join(dir, stagedMarkerName)); err != nil {
		t.Fatalf("remove marker (simulating Stage's entry step): %v", err)
	}
	if err := drv.StageCredential(dir, sampleCredential("rt-2"), provider.AddMeta{Email: "rotate@example.com"}); err != nil {
		t.Fatalf("simulate StageCredential: %v", err)
	}
	// Crash point: never reaches Stage's final atomicWrite of the new marker.

	done, err = s.Complete(drv, key)
	if err != nil {
		t.Fatalf("Complete: %v", err)
	}
	if done {
		t.Fatal("Complete should be false when a re-Stage was interrupted after invalidating the old marker but before writing the new one")
	}
}

// --- State / Reconcile ------------------------------------------------

func TestStateAbsentBeforeAnyStage(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	key := AccountKey("never-staged@example.com")
	if _, err := s.Create(drv.Name(), key, Template{}); err != nil {
		t.Fatalf("Create: %v", err)
	}
	st, err := s.State(drv, key)
	if err != nil {
		t.Fatalf("State: %v", err)
	}
	if st != StateAbsent {
		t.Fatalf("State = %v, want StateAbsent for a Create()-only profile", st)
	}
}

func TestStateStagedAfterStage(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	key := AccountKey("staged@example.com")
	if err := s.Stage(drv, key, sampleCredential("rt-staged"), provider.AddMeta{Email: "staged@example.com"}); err != nil {
		t.Fatalf("Stage: %v", err)
	}
	st, err := s.State(drv, key)
	if err != nil {
		t.Fatalf("State: %v", err)
	}
	if st != StateStaged {
		t.Fatalf("State = %v, want StateStaged right after a successful Stage", st)
	}
}

// TestStateRotatedIsNotIncompleteOrAbsent is the P2-review-①-mandated case:
// a credential the vendor's own runner rotated in place (fingerprint moved,
// but still carries real material) must classify as StateRotated, NOT
// StateAbsent/StateIncomplete — those would make a healthy, freshly-rotated
// account look broken (see Complete's CAUTION doc).
func TestStateRotatedIsNotIncompleteOrAbsent(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	key := AccountKey("rotated@example.com")
	if err := s.Stage(drv, key, sampleCredential("rt-old"), provider.AddMeta{Email: "rotated@example.com"}); err != nil {
		t.Fatalf("Stage: %v", err)
	}
	dir, _, err := s.resolveProfile(drv.Name(), key)
	if err != nil {
		t.Fatalf("resolveProfile: %v", err)
	}
	// Simulate the vendor's runner rotating the refresh token in place —
	// exactly what Complete's !Complete-after-rotation case models.
	if err := drv.StageCredential(dir, sampleCredential("rt-new"), provider.AddMeta{Email: "rotated@example.com"}); err != nil {
		t.Fatalf("simulate in-place rotation: %v", err)
	}
	st, err := s.State(drv, key)
	if err != nil {
		t.Fatalf("State: %v", err)
	}
	if st != StateRotated {
		t.Fatalf("State = %v, want StateRotated for a healthy in-place rotation", st)
	}
	// Sanity: this is exactly the scenario where naive !Complete would read
	// "not ready" — assert that trap is real so this test would fail if
	// someone reintroduced it as the readiness signal.
	if done, err := s.Complete(drv, key); err != nil || done {
		t.Fatalf("Complete after rotation = (%v, %v), want (false, nil) — confirms State, not Complete, is the readiness signal to use", done, err)
	}
}

func TestStateIncompleteForBlankCredential(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	key := AccountKey("blank@example.com")
	dir, err := s.Create(drv.Name(), key, Template{})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	// A logged-out shell: the token keys exist but are blank.
	blank := []byte(`{"claudeAiOauth":{"accessToken":"","refreshToken":""}}`)
	if err := os.WriteFile(drv.CredentialPath(dir), blank, 0o600); err != nil {
		t.Fatalf("write blank credential: %v", err)
	}
	st, err := s.State(drv, key)
	if err != nil {
		t.Fatalf("State: %v", err)
	}
	if st != StateIncomplete {
		t.Fatalf("State = %v, want StateIncomplete for a logged-out (blank) credential", st)
	}
}

func TestReconcileRestampsMarkerOnRotationAndBecomesStaged(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	key := AccountKey("reconcile@example.com")
	if err := s.Stage(drv, key, sampleCredential("rt-before"), provider.AddMeta{Email: "reconcile@example.com"}); err != nil {
		t.Fatalf("Stage: %v", err)
	}
	dir, _, err := s.resolveProfile(drv.Name(), key)
	if err != nil {
		t.Fatalf("resolveProfile: %v", err)
	}
	if err := drv.StageCredential(dir, sampleCredential("rt-after"), provider.AddMeta{Email: "reconcile@example.com"}); err != nil {
		t.Fatalf("simulate in-place rotation: %v", err)
	}
	if st, err := s.State(drv, key); err != nil || st != StateRotated {
		t.Fatalf("precondition: State = (%v, %v), want (StateRotated, nil)", st, err)
	}

	st, err := s.Reconcile(drv, key)
	if err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	if st != StateStaged {
		t.Fatalf("Reconcile returned %v, want StateStaged", st)
	}

	// The marker must now match the NEW (post-rotation) fingerprint, so a
	// plain re-read of State/Complete agrees without Reconcile running again.
	if st, err := s.State(drv, key); err != nil || st != StateStaged {
		t.Fatalf("State after Reconcile = (%v, %v), want (StateStaged, nil)", st, err)
	}
	if done, err := s.Complete(drv, key); err != nil || !done {
		t.Fatalf("Complete after Reconcile = (%v, %v), want (true, nil)", done, err)
	}
	markerFP, err := os.ReadFile(filepath.Join(dir, stagedMarkerName))
	if err != nil {
		t.Fatalf("read marker: %v", err)
	}
	if string(markerFP) != drv.Fingerprint(sampleCredential("rt-after")) {
		t.Fatalf("marker = %q, want the fingerprint of the rotated (post-rotation) credential", markerFP)
	}
}

func TestReconcileIsNoOpOnAbsentAndIncomplete(t *testing.T) {
	s := openStore(t)
	drv := claude.New()

	absentKey := AccountKey("reconcile-absent@example.com")
	if _, err := s.Create(drv.Name(), absentKey, Template{}); err != nil {
		t.Fatalf("Create: %v", err)
	}
	if st, err := s.Reconcile(drv, absentKey); err != nil || st != StateAbsent {
		t.Fatalf("Reconcile(absent) = (%v, %v), want (StateAbsent, nil)", st, err)
	}

	incompleteKey := AccountKey("reconcile-incomplete@example.com")
	dir, err := s.Create(drv.Name(), incompleteKey, Template{})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	blank := []byte(`{"claudeAiOauth":{"accessToken":"","refreshToken":""}}`)
	if err := os.WriteFile(drv.CredentialPath(dir), blank, 0o600); err != nil {
		t.Fatalf("write blank credential: %v", err)
	}
	if st, err := s.Reconcile(drv, incompleteKey); err != nil || st != StateIncomplete {
		t.Fatalf("Reconcile(incomplete) = (%v, %v), want (StateIncomplete, nil)", st, err)
	}
	if _, err := os.Stat(filepath.Join(dir, stagedMarkerName)); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("Reconcile must not write a marker for an incomplete credential: stat err = %v", err)
	}
}

func TestReconcileAlreadyStagedIsIdempotent(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	key := AccountKey("already-staged@example.com")
	if err := s.Stage(drv, key, sampleCredential("rt-idem"), provider.AddMeta{Email: "already-staged@example.com"}); err != nil {
		t.Fatalf("Stage: %v", err)
	}
	st, err := s.Reconcile(drv, key)
	if err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	if st != StateStaged {
		t.Fatalf("Reconcile on an already-staged profile = %v, want StateStaged", st)
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
	key := AccountKey("acct1@example.com")
	dir, err := s.Create("claude", key, Template{})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	if err := s.SetActive("claude", key); err != nil {
		t.Fatalf("SetActive: %v", err)
	}
	got, gotDir, err := s.GetActive("claude")
	if err != nil {
		t.Fatalf("GetActive: %v", err)
	}
	if got != key || gotDir != dir {
		t.Fatalf("GetActive = (%q, %q), want (%q, %q)", got, gotDir, key, dir)
	}
}

func TestSetActiveIsAtomic(t *testing.T) {
	s := openStore(t)
	key1 := AccountKey("acct1@example.com")
	key2 := AccountKey("acct2@example.com")
	if _, err := s.Create("claude", key1, Template{}); err != nil {
		t.Fatalf("Create acct1: %v", err)
	}
	if _, err := s.Create("claude", key2, Template{}); err != nil {
		t.Fatalf("Create acct2: %v", err)
	}
	if err := s.SetActive("claude", key1); err != nil {
		t.Fatalf("SetActive acct1: %v", err)
	}
	providerDir, err := s.resolveProviderDir("claude")
	if err != nil {
		t.Fatalf("resolveProviderDir: %v", err)
	}
	pointerPath := filepath.Join(providerDir, activeFileName)
	before, err := os.Stat(pointerPath)
	if err != nil {
		t.Fatalf("stat pointer: %v", err)
	}
	if err := s.SetActive("claude", key2); err != nil {
		t.Fatalf("SetActive acct2: %v", err)
	}
	after, err := os.Stat(pointerPath)
	if err != nil {
		t.Fatalf("stat pointer after: %v", err)
	}
	if os.SameFile(before, after) {
		t.Fatalf("pointer file was modified in place, not replaced atomically via rename")
	}
	got, _, err := s.GetActive("claude")
	if err != nil || got != key2 {
		t.Fatalf("GetActive after second SetActive = (%q, %v), want %q", got, err, key2)
	}
}

// TestSetActiveTightensPreExistingWideProviderDir is N6's exact reproduction:
// only Create/Stage self-healed a wide provider directory before this fix; a
// provider directory whose only prior touch was a pre-existing wide MkdirAll
// (never a Create/Stage) stayed wide through SetActive.
func TestSetActiveTightensPreExistingWideProviderDir(t *testing.T) {
	s := openStore(t)
	providerDir, err := s.resolveProviderDir("claude")
	if err != nil {
		t.Fatalf("resolveProviderDir: %v", err)
	}
	if err := os.MkdirAll(providerDir, 0o777); err != nil {
		t.Fatalf("pre-create wide provider dir: %v", err)
	}
	if err := os.Chmod(providerDir, 0o777); err != nil {
		t.Fatalf("chmod wide: %v", err)
	}

	if err := s.SetActive("claude", AccountKey("wide-setactive@example.com")); err != nil {
		t.Fatalf("SetActive: %v", err)
	}
	info, err := os.Stat(providerDir)
	if err != nil {
		t.Fatalf("stat provider dir: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0o700 {
		t.Fatalf("provider dir perm after SetActive = %o, want tightened to 0700", perm)
	}
}

func TestGetActivePointsToMissingProfile(t *testing.T) {
	s := openStore(t)
	key := AccountKey("acct1@example.com")
	if _, err := s.Create("claude", key, Template{}); err != nil {
		t.Fatalf("Create: %v", err)
	}
	if err := s.SetActive("claude", key); err != nil {
		t.Fatalf("SetActive: %v", err)
	}
	if err := s.Remove("claude", key); err != nil {
		t.Fatalf("Remove: %v", err)
	}
	got, dir, err := s.GetActive("claude")
	if !errors.Is(err, ErrActiveMissing) {
		t.Fatalf("GetActive after removing the active profile: err = %v, want ErrActiveMissing", err)
	}
	if got != key {
		t.Fatalf("GetActive should still report the missing key, got %q want %q", got, key)
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
	keyA := AccountKey("a-acct@example.com")
	keyB := AccountKey("b-acct@example.com")
	for _, k := range []string{keyB, keyA} {
		if _, err := s.Create("claude", k, Template{}); err != nil {
			t.Fatalf("Create %s: %v", k, err)
		}
	}
	if err := s.SetActive("claude", keyA); err != nil {
		t.Fatalf("SetActive: %v", err)
	}
	keys, err := s.List("claude")
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	want := []string{keyA, keyB}
	sortedWant := append([]string{}, want...)
	if keyA > keyB {
		sortedWant = []string{keyB, keyA}
	}
	if len(keys) != 2 || keys[0] != sortedWant[0] || keys[1] != sortedWant[1] {
		t.Fatalf("List = %v, want sorted %v (activeFileName excluded)", keys, sortedWant)
	}
}

// --- Lock: contention must actually prevent the write/delete --------------

func TestStageBlockedByHeldLockDoesNotWriteCredential(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	key := AccountKey("locked@example.com")
	dir, err := s.Create(drv.Name(), key, Template{})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	providerDir := filepath.Dir(dir)
	held, err := fslock.TryLock(s.lockPath(providerDir, key))
	if err != nil {
		t.Fatalf("TryLock: %v", err)
	}
	defer held.Unlock()

	err = s.Stage(drv, key, sampleCredential("rt"), provider.AddMeta{Email: "locked@example.com"})
	if err == nil {
		t.Fatal("Stage should fail while the profile lock is held elsewhere")
	}
	if _, statErr := os.Stat(drv.CredentialPath(dir)); !errors.Is(statErr, os.ErrNotExist) {
		t.Fatalf("Stage wrote the credential file despite the lock being held: statErr=%v", statErr)
	}
}

func TestRemoveBlockedByHeldLockLeavesProfileIntact(t *testing.T) {
	s := openStore(t)
	key := AccountKey("locked-remove@example.com")
	dir, err := s.Create("claude", key, Template{})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "still-here.txt"), []byte("x"), 0o600); err != nil {
		t.Fatalf("seed file: %v", err)
	}
	providerDir := filepath.Dir(dir)
	held, err := fslock.TryLock(s.lockPath(providerDir, key))
	if err != nil {
		t.Fatalf("TryLock: %v", err)
	}
	defer held.Unlock()

	if err := s.Remove("claude", key); err == nil {
		t.Fatal("Remove should fail while the profile lock is held elsewhere")
	}
	if _, statErr := os.Stat(filepath.Join(dir, "still-here.txt")); statErr != nil {
		t.Fatalf("Remove deleted the profile despite the lock being held: %v", statErr)
	}
}

// TestStageRejectsSymlinkedLockPath is N4's exact reproduction: a symlink
// planted at <providerDir>/<accountKey>.lock lets fslock's O_CREATE open
// follow it and lock/create a file wherever the link points, outside the
// store root entirely.
func TestStageRejectsSymlinkedLockPath(t *testing.T) {
	if runtimeIsWindows() {
		t.Skip("symlink creation semantics differ on windows; covered by unix CI")
	}
	s := openStore(t)
	drv := claude.New()
	key := AccountKey("lock-symlink@example.com")
	dir, err := s.Create(drv.Name(), key, Template{})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	providerDir := filepath.Dir(dir)
	elsewhere := filepath.Join(t.TempDir(), "elsewhere")
	if err := os.MkdirAll(elsewhere, 0o700); err != nil {
		t.Fatalf("mkdir elsewhere: %v", err)
	}
	evilLock := filepath.Join(elsewhere, "hijacked.lock")
	if err := os.Symlink(evilLock, s.lockPath(providerDir, key)); err != nil {
		t.Fatalf("symlink lock path: %v", err)
	}

	err = s.Stage(drv, key, sampleCredential("rt"), provider.AddMeta{Email: "lock-symlink@example.com"})
	if err == nil {
		t.Fatal("Stage should reject a symlinked lock path")
	}
	if _, statErr := os.Stat(evilLock); !errors.Is(statErr, os.ErrNotExist) {
		t.Fatalf("lock file was created through the symlink outside the store: %v", statErr)
	}
}

// TestRemoveRejectsDirectoryAtLockPath is N4's other half: a directory
// planted at the lock path (instead of a symlink) must be rejected rather
// than wedging every future Stage/Remove on this profile permanently.
func TestRemoveRejectsDirectoryAtLockPath(t *testing.T) {
	s := openStore(t)
	key := AccountKey("lock-dir@example.com")
	dir, err := s.Create("claude", key, Template{})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	providerDir := filepath.Dir(dir)
	if err := os.MkdirAll(s.lockPath(providerDir, key), 0o700); err != nil {
		t.Fatalf("plant directory at lock path: %v", err)
	}

	if err := s.Remove("claude", key); err == nil {
		t.Fatal("Remove should reject a non-regular file at the lock path instead of hanging or silently ignoring it")
	}
	if _, statErr := os.Stat(dir); statErr != nil {
		t.Fatalf("profile should remain intact when Remove is rejected for a bad lock path: %v", statErr)
	}
}

// --- Env / Path --------------------------------------------------------

func TestEnvDelegatesToDriver(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	key := AccountKey("env@example.com")
	dir, err := s.Create(drv.Name(), key, Template{})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	env, err := s.Env(drv, key)
	if err != nil {
		t.Fatalf("Env: %v", err)
	}
	// Assert the actual, concrete value the claude driver is documented to
	// produce (CLAUDE_CONFIG_DIR=<configDir>) rather than re-deriving it via
	// drv.Env and comparing two calls to the same function against each
	// other, which would pass regardless of what Store.Env's own path
	// resolution did.
	want := "CLAUDE_CONFIG_DIR=" + dir
	if len(env) != 1 || env[0] != want {
		t.Fatalf("Env = %v, want [%q]", env, want)
	}
}

func TestEnvRejectsInvalidAccountKey(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	if _, err := s.Env(drv, "not-a-valid-key"); err == nil {
		t.Fatal("Env with an invalid accountKey should be rejected")
	}
}

func runtimeIsWindows() bool {
	return os.PathSeparator == '\\'
}
