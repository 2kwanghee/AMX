package claude

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
)

const (
	credV1     = `{"claudeAiOauth":{"accessToken":"a1","refreshToken":"r1"}}`
	credV2     = `{"claudeAiOauth":{"accessToken":"a2","refreshToken":"r2"}}`
	credV1Acc2 = `{"claudeAiOauth":{"accessToken":"a3","refreshToken":"r1"}}` // access rotated, refresh same
)

// TestWriteFileAtomic verifies the credential/identity writer is atomic (temp +
// rename): the final file holds the last full write, carries 0o600, and no
// partial temp file is left behind for the runner to read (B1a).
func TestWriteFileAtomic(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, ".credentials.json")

	if err := writeFileAtomic(path, []byte("first"), 0o600); err != nil {
		t.Fatalf("first write: %v", err)
	}
	// Overwrite an existing file (rename must replace it atomically).
	if err := writeFileAtomic(path, []byte("second-and-longer"), 0o600); err != nil {
		t.Fatalf("overwrite: %v", err)
	}

	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "second-and-longer" {
		t.Fatalf("content = %q, want the full second write (no torn/partial state)", got)
	}
	if perm := mustStat(t, path).Mode().Perm(); perm != 0o600 {
		t.Fatalf("perm = %o, want 600", perm)
	}

	// The rename must consume the temp file: nothing but the target may remain.
	ents, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range ents {
		if e.Name() != ".credentials.json" {
			t.Fatalf("stray file left in dir (temp not renamed): %s", e.Name())
		}
		if strings.Contains(e.Name(), ".amx-") {
			t.Fatalf("temp file leaked: %s", e.Name())
		}
	}
}

// TestStageCredentialMergesExistingClaudeJSON pins the merge behavior at
// claude.go:107-126: an existing .claude.json is read and merged, not
// replaced, so runner state the daemon never touches (machineID,
// firstStartTime, and any other key Claude Code keeps there) survives
// staging untouched. It also pins that a pre-existing theme is preserved
// rather than overwritten, and that oauthAccount/hasCompletedOnboarding are
// populated from AddMeta.
func TestStageCredentialMergesExistingClaudeJSON(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, ".claude.json")
	existing := `{"machineID":"m-123","firstStartTime":"2026-01-01T00:00:00Z","theme":"light","numStartups":42}`
	if err := os.WriteFile(configPath, []byte(existing), 0o600); err != nil {
		t.Fatal(err)
	}

	d := New()
	meta := provider.AddMeta{Email: "a@example.com", AccountUUID: "acc-1", OrganizationName: "Acme"}
	if err := d.StageCredential(dir, []byte(credV1), meta); err != nil {
		t.Fatalf("StageCredential: %v", err)
	}

	raw, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]json.RawMessage
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("staged .claude.json is not valid JSON: %v", err)
	}

	// Runner state must survive the merge untouched.
	wantSurvive := map[string]string{
		"machineID":      `"m-123"`,
		"firstStartTime": `"2026-01-01T00:00:00Z"`,
		"numStartups":    `42`,
	}
	for k, want := range wantSurvive {
		if string(got[k]) != want {
			t.Errorf("%s = %s, want %s (runner state must survive merge)", k, got[k], want)
		}
	}

	// theme already present -> preserved, not overwritten with the default.
	if string(got["theme"]) != `"light"` {
		t.Errorf("theme = %s, want preserved %q", got["theme"], "light")
	}

	if string(got["hasCompletedOnboarding"]) != "true" {
		t.Errorf("hasCompletedOnboarding = %s, want true", got["hasCompletedOnboarding"])
	}

	var oauth struct {
		EmailAddress     string `json:"emailAddress"`
		AccountUUID      string `json:"accountUuid"`
		OrganizationUUID string `json:"organizationUuid"`
		OrganizationName string `json:"organizationName"`
	}
	if err := json.Unmarshal(got["oauthAccount"], &oauth); err != nil {
		t.Fatalf("oauthAccount not valid JSON: %v", err)
	}
	if oauth.EmailAddress != meta.Email || oauth.AccountUUID != meta.AccountUUID || oauth.OrganizationName != meta.OrganizationName {
		t.Errorf("oauthAccount = %+v, want fields from AddMeta %+v", oauth, meta)
	}
	// AddMeta carries no organization UUID field at all (see provider.AddMeta);
	// claude.go never sets identity.OAuthAccount.OrganizationUUID, so it must
	// always serialize as the zero value.
	if oauth.OrganizationUUID != "" {
		t.Errorf("organizationUuid = %q, want empty (AddMeta has no such field)", oauth.OrganizationUUID)
	}
}

// TestStageCredentialFirstTimeNoExistingClaudeJSON pins first-time staging
// (no prior .claude.json): StageCredential must not error, and must default
// theme to "dark" and set hasCompletedOnboarding, since there is nothing to
// preserve.
func TestStageCredentialFirstTimeNoExistingClaudeJSON(t *testing.T) {
	dir := t.TempDir()
	d := New()
	if err := d.StageCredential(dir, []byte(credV1), provider.AddMeta{Email: "a@example.com"}); err != nil {
		t.Fatalf("StageCredential: %v", err)
	}
	raw, err := os.ReadFile(filepath.Join(dir, ".claude.json"))
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]json.RawMessage
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("staged .claude.json is not valid JSON: %v", err)
	}
	if string(got["theme"]) != `"dark"` {
		t.Errorf("theme = %s, want default %q when absent", got["theme"], "dark")
	}
	if string(got["hasCompletedOnboarding"]) != "true" {
		t.Errorf("hasCompletedOnboarding = %s, want true", got["hasCompletedOnboarding"])
	}
}

// TestStageCredentialDegradesCorruptClaudeJSON pins the documented failure
// mode at claude.go:110-111: an existing .claude.json that fails to parse
// degrades to a fresh map rather than aborting the stage. StageCredential
// must still succeed and the result must carry the onboarding defaults, with
// none of the corrupt content preserved (there is nothing valid to merge).
func TestStageCredentialDegradesCorruptClaudeJSON(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, ".claude.json")
	if err := os.WriteFile(configPath, []byte(`{not valid json`), 0o600); err != nil {
		t.Fatal(err)
	}
	d := New()
	if err := d.StageCredential(dir, []byte(credV1), provider.AddMeta{Email: "a@example.com"}); err != nil {
		t.Fatalf("StageCredential must tolerate a corrupt existing .claude.json, got: %v", err)
	}
	raw, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]json.RawMessage
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("staged .claude.json must be valid JSON even after a corrupt input, got: %v", err)
	}
	if string(got["hasCompletedOnboarding"]) != "true" {
		t.Errorf("hasCompletedOnboarding = %s, want true", got["hasCompletedOnboarding"])
	}
	if string(got["theme"]) != `"dark"` {
		t.Errorf("theme = %s, want default %q (fresh map, nothing to preserve)", got["theme"], "dark")
	}
}

// TestStageCredentialWritesCredentialBytesExactly pins that the credential
// blob lands in .credentials.json byte-for-byte, and that both staged files
// carry 0o600 (claude.go:92,126). Asserted by length, never by printing the
// credential content (§7).
func TestStageCredentialWritesCredentialBytesExactly(t *testing.T) {
	dir := t.TempDir()
	d := New()
	cred := []byte(credV1)
	if err := d.StageCredential(dir, cred, provider.AddMeta{Email: "a@example.com"}); err != nil {
		t.Fatalf("StageCredential: %v", err)
	}
	got, err := os.ReadFile(filepath.Join(dir, ".credentials.json"))
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != len(cred) {
		t.Errorf("credential file length = %d, want %d (bytes must round-trip exactly)", len(got), len(cred))
	}
	match := len(got) == len(cred)
	if match {
		for i := range got {
			if got[i] != cred[i] {
				match = false
				break
			}
		}
	}
	if !match {
		t.Error("credential file content does not match the staged bytes exactly")
	}
	if perm := mustStat(t, filepath.Join(dir, ".credentials.json")).Mode().Perm(); perm != 0o600 {
		t.Errorf(".credentials.json perm = %o, want 600", perm)
	}
	if perm := mustStat(t, filepath.Join(dir, ".claude.json")).Mode().Perm(); perm != 0o600 {
		t.Errorf(".claude.json perm = %o, want 600", perm)
	}
}

// TestStageCredentialCreatesConfigDir pins that a missing configDir is
// created (claude.go:85, MkdirAll 0o700) rather than StageCredential
// erroring out.
func TestStageCredentialCreatesConfigDir(t *testing.T) {
	base := t.TempDir()
	dir := filepath.Join(base, "nested", "config")
	d := New()
	if err := d.StageCredential(dir, []byte(credV1), provider.AddMeta{Email: "a@example.com"}); err != nil {
		t.Fatalf("StageCredential: %v", err)
	}
	fi, err := os.Stat(dir)
	if err != nil {
		t.Fatalf("configDir not created: %v", err)
	}
	if !fi.IsDir() {
		t.Fatal("configDir path is not a directory")
	}
	if perm := fi.Mode().Perm(); perm != 0o700 {
		t.Errorf("configDir perm = %o, want 700", perm)
	}
}

func mustStat(t *testing.T, path string) os.FileInfo {
	t.Helper()
	fi, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	return fi
}

// TestFingerprintMirrorsTsamx pins the fingerprint scheme against tsamx
// oauth.credential_fingerprint: refresh-token hash when present, content hash
// otherwise, and empty only for empty input.
// --- Identity -------------------------------------------------------------

func TestIdentityReadsBackStagedEmail(t *testing.T) {
	dir := t.TempDir()
	d := New()
	meta := provider.AddMeta{Email: "Reader@Example.com", AccountUUID: "acc-1", OrganizationName: "Acme"}
	if err := d.StageCredential(dir, []byte(credV1), meta); err != nil {
		t.Fatalf("StageCredential: %v", err)
	}
	got, err := d.Identity(dir)
	if err != nil {
		t.Fatalf("Identity: %v", err)
	}
	if got != meta.Email {
		t.Fatalf("Identity = %q, want the RAW staged email %q unchanged (no lowercasing)", got, meta.Email)
	}
}

func TestIdentityErrorsWhenNeverStaged(t *testing.T) {
	d := New()
	if _, err := d.Identity(t.TempDir()); err == nil {
		t.Fatal("Identity on a dir with no .claude.json should error, not return an empty email silently")
	}
}

func TestIdentityErrorsOnEmptyOAuthAccount(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, ".claude.json"), []byte(`{"theme":"dark"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	d := New()
	if _, err := d.Identity(dir); err == nil {
		t.Fatal("Identity on a .claude.json with no oauthAccount email should error")
	}
}

func TestFingerprintMirrorsTsamx(t *testing.T) {
	d := New()
	if d.Fingerprint(nil) != "" {
		t.Fatal("empty input must be empty fingerprint")
	}
	oauth := d.Fingerprint([]byte(credV1))
	if oauth[:7] != "sha256:" {
		t.Fatalf("oauth credential must use refresh-token hash: %q", oauth)
	}
	// Same refresh token, different access token -> equal fingerprint.
	if d.Fingerprint([]byte(credV1Acc2)) != oauth {
		t.Fatal("fingerprint must ignore access-token rotation")
	}
	// Different refresh token -> different fingerprint.
	if d.Fingerprint([]byte(credV2)) == oauth {
		t.Fatal("fingerprint must change on refresh-token rotation")
	}
	// Non-OAuth (API key) -> content hash.
	full := d.Fingerprint([]byte(`{"apiKey":"sk-xyz"}`))
	if full[:12] != "sha256-full:" {
		t.Fatalf("non-oauth credential must use content hash: %q", full)
	}
}

// TestHasCredentialMaterial pins the conservative judgement: false ONLY for a
// definitely token-less set (a token key present but every present one blank, or a
// blank body), true for every shape that cannot be judged (opaque api_key, unknown
// schema outside OR inside the claudeAiOauth block, non-string token).
//
// This table is mirrored case-for-case, in the same order, by
// ams-server/tests/test_credential_resync.py::CLAUDE_MATERIAL_CASES. The two
// implementations must agree on every row (a Go true + AMS false would advance the
// AMA baseline on a push AMS refuses), so a row added here belongs there too.
func TestHasCredentialMaterial(t *testing.T) {
	d := New()
	cases := []struct {
		name string
		in   string
		want bool
	}{
		{"both tokens", credV1, true},
		{"access token only", `{"claudeAiOauth":{"accessToken":"a1","refreshToken":""}}`, true},
		{"refresh token only", `{"claudeAiOauth":{"accessToken":"","refreshToken":"r1"}}`, true},
		{"setup-token shape (no refreshToken key)", `{"claudeAiOauth":{"accessToken":"sk-ant-oat-x"}}`, true},
		{"empty token set", `{"claudeAiOauth":{"accessToken":"","refreshToken":"","expiresAt":0}}`, false},
		{"whitespace-only tokens", `{"claudeAiOauth":{"accessToken":"  ","refreshToken":"\t\n"}}`, false},
		// U+001C/U+0000 are blank to Python's str.strip() but not to unicode.IsSpace;
		// both sides use (space OR Cc) so this row reads false on both.
		{"control-char-only tokens", `{"claudeAiOauth":{"accessToken":"\u001c","refreshToken":"\u0000"}}`, false},
		{"null tokens", `{"claudeAiOauth":{"accessToken":null,"refreshToken":null}}`, false},
		{"one token key present and blank", `{"claudeAiOauth":{"accessToken":""}}`, false},
		// The three rows below were false before the 2026-08-17 review: an unknown
		// schema INSIDE claudeAiOauth is as unjudgeable as one outside it, and
		// permanently dropping such a set would strand the account. The real failure
		// mode is unaffected — the observed logged-out shell has BOTH keys present
		// with "" values (row "empty token set").
		{"unknown keys inside the block", `{"claudeAiOauth":{"token":"abc","expiresAt":1}}`, true},
		{"no token keys at all", `{"claudeAiOauth":{"expiresAt":0}}`, true},
		{"empty claudeAiOauth object", `{"claudeAiOauth":{}}`, true},
		{"no claudeAiOauth key", `{"apiKey":"sk-xyz"}`, true},
		{"empty object", `{}`, true},
		{"claudeAiOauth not an object", `{"claudeAiOauth":"opaque"}`, true},
		{"claudeAiOauth null", `{"claudeAiOauth":null}`, true},
		{"non-string token", `{"claudeAiOauth":{"accessToken":123,"refreshToken":""}}`, true},
		{"not JSON (opaque api key)", `sk-ant-api03-opaque`, true},
		{"JSON array", `[1,2,3]`, true},
		{"JSON string", `"just-a-string"`, true},
		// A blank body cannot be any credential, opaque api_key included, so unlike
		// the non-JSON rows above it is refused rather than waved through.
		{"empty input", ``, false},
		{"whitespace-only body", "   \n\t", false},
		{"control-char-only body", "\x00\x1f", false},
	}
	for _, c := range cases {
		if got := d.HasCredentialMaterial([]byte(c.in)); got != c.want {
			t.Errorf("%s: HasCredentialMaterial = %v, want %v", c.name, got, c.want)
		}
	}
}
