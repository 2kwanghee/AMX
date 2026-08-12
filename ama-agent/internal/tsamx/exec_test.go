package tsamx

import (
	"context"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider/claude"
)

// TestDeliverLockExclusive verifies DeliverLock takes a real exclusive flock over
// <configDir>/.amx-deliver.lock: while held, a second LOCK_EX attempt fails, and
// after release it succeeds (B1b — the runner's shared lock blocks likewise).
func TestDeliverLockExclusive(t *testing.T) {
	dir := t.TempDir()
	b := &ExecBridge{ConfigDir: dir}

	release := b.DeliverLock(context.Background())
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

// TestDeliverLockFailOpen (B1b review item 1b): when the lock is already held by
// another holder (a long-lived runner), DeliverLock must NOT block indefinitely —
// it retries up to LockMaxWait then returns a no-op release so the deliver
// proceeds unlocked (fail-open). We verify it gives up within a bound and does not
// steal the lock (the other holder still owns it afterward).
func TestDeliverLockFailOpen(t *testing.T) {
	dir := t.TempDir()
	lockPath := filepath.Join(dir, deliverLockName)

	// Another process/holder owns the lock (models a running runner).
	holder, err := os.OpenFile(lockPath, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer holder.Close()
	if err := syscall.Flock(int(holder.Fd()), syscall.LOCK_EX); err != nil {
		t.Fatal(err)
	}

	b := &ExecBridge{ConfigDir: dir, LockMaxWait: 150 * time.Millisecond}
	start := time.Now()
	release := b.DeliverLock(context.Background())
	elapsed := time.Since(start)

	if release == nil {
		t.Fatal("DeliverLock returned a nil release (must always be usable)")
	}
	if elapsed < 100*time.Millisecond {
		t.Fatalf("DeliverLock returned too fast (%v) — did it wait out the bound?", elapsed)
	}
	if elapsed > 3*time.Second {
		t.Fatalf("DeliverLock blocked far past its bound (%v) — not fail-open", elapsed)
	}
	// The holder must still own the lock — fail-open does not steal it.
	if err := syscall.Flock(int(holder.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		t.Fatalf("holder lost the lock after DeliverLock fail-open: %v", err)
	}
	_ = release() // no-op release must be safe to call
}

// TestDeliverLockDefaultsToClaudeHome (B1b review item 3): with no ConfigDir,
// DeliverLock resolves ~/.claude (matching the amx-claude wrapper's default) so
// both sides flock the SAME file. We point HOME at a temp dir and check the lock
// lands under <HOME>/.claude.
func TestDeliverLockDefaultsToClaudeHome(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)

	// ConfigDir empty -> must fall back to the driver's conventional home
	// (claude: ~/.claude). Fallback now lives in Driver.DefaultConfigHome.
	b := &ExecBridge{Driver: claude.New()}
	release := b.DeliverLock(context.Background())
	defer release()

	lockPath := filepath.Join(home, ".claude", deliverLockName)
	if _, err := os.Stat(lockPath); err != nil {
		t.Fatalf("expected lock under ~/.claude (%s): %v", lockPath, err)
	}
}

// TestDeliverLockNoHomeIsNoop: with neither a config home nor a resolvable HOME,
// DeliverLock returns a usable no-op release and takes no lock.
func TestDeliverLockNoHomeIsNoop(t *testing.T) {
	t.Setenv("HOME", "")
	b := &ExecBridge{}
	release := b.DeliverLock(context.Background())
	if release == nil {
		t.Fatal("release must never be nil")
	}
	if err := release(); err != nil {
		t.Fatalf("no-op release err: %v", err)
	}
}
