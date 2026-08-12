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
