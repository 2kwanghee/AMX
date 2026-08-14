package fslock

import (
	"errors"
	"path/filepath"
	"testing"
)

// TestTryLockContended verifies a second acquisition of the same lock file (a
// separate fd in the same process) reports the lock as held rather than
// succeeding. On unix the second attempt returns ErrWouldBlock directly.
func TestTryLockContended(t *testing.T) {
	path := filepath.Join(t.TempDir(), ".lock")

	first, err := TryLock(path)
	if err != nil {
		t.Fatalf("first TryLock: %v", err)
	}
	defer first.Unlock()

	second, err := TryLock(path)
	if err == nil {
		_ = second.Unlock()
		t.Fatal("second TryLock acquired the held lock (not exclusive)")
	}
	if !errors.Is(err, ErrWouldBlock) {
		t.Fatalf("contended TryLock err = %v, want ErrWouldBlock", err)
	}
}

// TestTryLockReacquireAfterUnlock verifies the lock is free again once released.
func TestTryLockReacquireAfterUnlock(t *testing.T) {
	path := filepath.Join(t.TempDir(), ".lock")

	first, err := TryLock(path)
	if err != nil {
		t.Fatalf("first TryLock: %v", err)
	}
	if err := first.Unlock(); err != nil {
		t.Fatalf("Unlock: %v", err)
	}

	second, err := TryLock(path)
	if err != nil {
		t.Fatalf("re-acquire after Unlock should succeed, got %v", err)
	}
	_ = second.Unlock()
}

// TestTryLockMissingParentDir verifies that a lock path whose parent directory
// does not exist fails at the OpenFile step with a real error — NOT ErrWouldBlock,
// which must mean only "held by someone else". Callers fail-open on such errors.
func TestTryLockMissingParentDir(t *testing.T) {
	path := filepath.Join(t.TempDir(), "no-such-dir", ".lock")

	l, err := TryLock(path)
	if err == nil {
		_ = l.Unlock()
		t.Fatal("TryLock under a missing parent dir must fail")
	}
	if errors.Is(err, ErrWouldBlock) {
		t.Fatalf("missing parent dir must not report ErrWouldBlock, got %v", err)
	}
}
