package tsamx

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
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
