package claude

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
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
