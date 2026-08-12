package codex

import (
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
