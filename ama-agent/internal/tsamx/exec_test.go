package tsamx

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"syscall"
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

// TestDeliverLockExclusive verifies DeliverLock takes a real exclusive flock over
// <configDir>/.amx-deliver.lock: while held, a second LOCK_EX attempt fails, and
// after release it succeeds (B1b — the runner's shared lock blocks likewise).
func TestDeliverLockExclusive(t *testing.T) {
	dir := t.TempDir()
	b := &ExecBridge{ConfigDir: dir}

	release, err := b.DeliverLock(context.Background())
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}
	lockPath := filepath.Join(dir, deliverLockName)
	if _, err := os.Stat(lockPath); err != nil {
		t.Fatalf("lock file not created: %v", err)
	}

	f2, err := os.OpenFile(lockPath, os.O_RDWR, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer f2.Close()
	// A non-blocking exclusive attempt must fail while the deliver lock is held.
	if err := syscall.Flock(int(f2.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err == nil {
		_ = syscall.Flock(int(f2.Fd()), syscall.LOCK_UN)
		t.Fatal("second LOCK_EX acquired while deliver lock held (lock is not exclusive)")
	}

	if err := release(); err != nil {
		t.Fatalf("release: %v", err)
	}
	// After release the same attempt succeeds.
	if err := syscall.Flock(int(f2.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		t.Fatalf("LOCK_EX after release should succeed: %v", err)
	}
	_ = syscall.Flock(int(f2.Fd()), syscall.LOCK_UN)
}

// TestDeliverLockNoConfigDirIsNoop: with no config home there is nothing to
// protect, so DeliverLock returns a no-op release and no error.
func TestDeliverLockNoConfigDirIsNoop(t *testing.T) {
	b := &ExecBridge{}
	release, err := b.DeliverLock(context.Background())
	if err != nil {
		t.Fatalf("no-config DeliverLock err: %v", err)
	}
	if err := release(); err != nil {
		t.Fatalf("no-op release err: %v", err)
	}
}
