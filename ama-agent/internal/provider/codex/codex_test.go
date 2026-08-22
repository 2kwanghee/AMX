package codex

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
)

const (
	authV1     = `{"auth_mode":"chatgpt","tokens":{"id_token":"i1","access_token":"a1","refresh_token":"r1","account_id":"acc1"}}`
	authV1Acc2 = `{"auth_mode":"chatgpt","tokens":{"id_token":"i9","access_token":"a9","refresh_token":"r1","account_id":"acc1"}}` // access/id rotated, refresh same
	authV2     = `{"auth_mode":"chatgpt","tokens":{"id_token":"i2","access_token":"a2","refresh_token":"r2","account_id":"acc1"}}`
)

// --- Identity ---------------------------------------------------------

func TestIdentityReadsBackBridgeMetaSidecar(t *testing.T) {
	dir := t.TempDir()
	// Driver.StageCredential never writes metaFile (see codex.go doc: identity
	// lives only in the bridge's sidecar), so this test writes it the way
	// Bridge.Add does rather than going through StageCredential.
	blob, err := json.Marshal(codexMeta{Email: "Codex.User@Example.com", AccountUUID: "acc-1", OrganizationName: "Acme"})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, metaFile), blob, 0o600); err != nil {
		t.Fatal(err)
	}
	d := New()
	got, err := d.Identity(dir)
	if err != nil {
		t.Fatalf("Identity: %v", err)
	}
	if got != "Codex.User@Example.com" {
		t.Fatalf("Identity = %q, want the raw sidecar email unchanged", got)
	}
}

func TestIdentityErrorsWhenNoSidecar(t *testing.T) {
	d := New()
	if _, err := d.Identity(t.TempDir()); err == nil {
		t.Fatal("Identity with no meta sidecar should error, not return an empty email silently")
	}
}

// TestFingerprint pins the scheme: refresh-token hash when present (surviving
// access/id-token rotation), content hash otherwise, empty only for empty input.
func TestFingerprint(t *testing.T) {
	d := New()
	if d.Fingerprint(nil) != "" {
		t.Fatal("empty input must be empty fingerprint")
	}
	fp := d.Fingerprint([]byte(authV1))
	if fp[:7] != "sha256:" {
		t.Fatalf("token credential must use refresh-token hash: %q", fp)
	}
	if d.Fingerprint([]byte(authV1Acc2)) != fp {
		t.Fatal("fingerprint must ignore access/id-token rotation")
	}
	if d.Fingerprint([]byte(authV2)) == fp {
		t.Fatal("fingerprint must change on refresh-token rotation")
	}
	full := d.Fingerprint([]byte(`{"auth_mode":"apikey","OPENAI_API_KEY":"sk-x","tokens":null}`))
	if full[:12] != "sha256-full:" {
		t.Fatalf("credential without refresh token must use content hash: %q", full)
	}
}

// TestHasCredentialMaterial pins the conservative judgement: false ONLY for a
// definitely token-less body (a token key present but every present one blank and
// no API key, or a blank body), true for every shape that cannot be judged
// (unknown schema outside OR inside the tokens block, non-object tokens,
// non-string token).
//
// This table is mirrored case-for-case, in the same order, by
// ams-server/tests/test_credential_resync.py::CODEX_MATERIAL_CASES. The two
// implementations must agree on every row (a Go true + AMS false would advance the
// AMA baseline on a push AMS refuses), so a row added here belongs there too.
func TestHasCredentialMaterial(t *testing.T) {
	d := New()
	cases := []struct {
		name string
		in   string
		want bool
	}{
		{"full token set", authV1, true},
		{"access token only", `{"tokens":{"refresh_token":"","access_token":"a1"}}`, true},
		{"refresh token only", `{"tokens":{"refresh_token":"r1","access_token":""}}`, true},
		{"empty token set", `{"auth_mode":"chatgpt","tokens":{"refresh_token":"","access_token":"","id_token":""}}`, false},
		{"whitespace-only tokens", `{"tokens":{"refresh_token":" ","access_token":"\t"}}`, false},
		// U+001F/U+0000 are blank to Python's str.strip() but not to unicode.IsSpace;
		// both sides use (space OR Cc) so this row reads false on both.
		{"control-char-only tokens", `{"tokens":{"refresh_token":"\u001f","access_token":"\u0000"}}`, false},
		{"null tokens values", `{"tokens":{"refresh_token":null,"access_token":null}}`, false},
		{"one token key present and blank", `{"tokens":{"refresh_token":""}}`, false},
		// The three rows below were false before the 2026-08-17 review: an unknown
		// schema INSIDE tokens is as unjudgeable as one outside it, and permanently
		// dropping such a body would strand the account. The real failure mode is
		// unaffected — a logged-out auth.json keeps its token keys with "" values
		// (row "empty token set").
		{"unknown keys inside tokens", `{"tokens":{"token":"abc","account_id":"acc1"}}`, true},
		{"no token keys at all", `{"tokens":{"account_id":"acc1"}}`, true},
		{"empty tokens object", `{"tokens":{}}`, true},
		{"empty tokens but api key", `{"OPENAI_API_KEY":"sk-x","tokens":{"refresh_token":"","access_token":""}}`, true},
		{"empty tokens and blank api key", `{"OPENAI_API_KEY":"  ","tokens":{"refresh_token":"","access_token":""}}`, false},
		{"api-key form (tokens null)", `{"auth_mode":"apikey","OPENAI_API_KEY":"sk-x","tokens":null}`, true},
		{"no tokens key", `{"OPENAI_API_KEY":"sk-x"}`, true},
		{"empty object", `{}`, true},
		{"non-string token", `{"tokens":{"refresh_token":123,"access_token":""}}`, true},
		{"not JSON", `not-json-at-all`, true},
		{"JSON array", `[1,2,3]`, true},
		// A blank body cannot be any credential, opaque api key included, so unlike
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

// TestStageCredentialPerms verifies auth.json is written 0600 with the exact body
// and no separate onboarding/identity file is created (Codex reads none).
func TestStageCredentialPerms(t *testing.T) {
	d := New()
	dir := t.TempDir()
	if err := d.StageCredential(dir, []byte(authV1), provider.AddMeta{Email: "u@x"}); err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(filepath.Join(dir, "auth.json"))
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != authV1 {
		t.Fatalf("auth.json body = %q, want staged credential verbatim", got)
	}
	fi, err := os.Stat(filepath.Join(dir, "auth.json"))
	if err != nil {
		t.Fatal(err)
	}
	if perm := fi.Mode().Perm(); perm != 0o600 {
		t.Fatalf("auth.json perm = %o, want 600", perm)
	}
	ents, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(ents) != 1 || ents[0].Name() != "auth.json" {
		t.Fatalf("StageCredential must write only auth.json, got %v", names(ents))
	}
}

func names(ents []os.DirEntry) []string {
	var out []string
	for _, e := range ents {
		out = append(out, e.Name())
	}
	return out
}
