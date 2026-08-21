package tsamx

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/fslock"
	"github.com/2kwanghee/AMX/ama-agent/internal/provider/claude"
)

// TestDeliverLockExclusive verifies DeliverLock takes a real exclusive lock over
// <configDir>/.amx-deliver.lock: while held, a second non-blocking attempt fails
// (ErrWouldBlock), and after release it succeeds (B1b — the runner's shared lock
// blocks likewise). The probe uses fslock so the check is the same primitive on
// every platform.
func TestDeliverLockExclusive(t *testing.T) {
	dir := t.TempDir()
	b := &ExecBridge{ConfigDir: dir}

	release, failOpen := b.DeliverLock(context.Background())
	if failOpen {
		t.Fatal("lock was acquired uncontended; failOpen must be false")
	}
	lockPath := filepath.Join(dir, deliverLockName)
	if _, err := os.Stat(lockPath); err != nil {
		t.Fatalf("lock file not created: %v", err)
	}

	// A non-blocking exclusive attempt must fail while the deliver lock is held.
	if l, err := fslock.TryLock(lockPath); !errors.Is(err, fslock.ErrWouldBlock) {
		if l != nil {
			_ = l.Unlock()
		}
		t.Fatalf("second lock acquired while deliver lock held (not exclusive): err=%v", err)
	}

	if err := release(); err != nil {
		t.Fatalf("release: %v", err)
	}
	// After release the same attempt succeeds.
	l2, err := fslock.TryLock(lockPath)
	if err != nil {
		t.Fatalf("lock after release should succeed: %v", err)
	}
	_ = l2.Unlock()
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
	holder, err := fslock.TryLock(lockPath)
	if err != nil {
		t.Fatal(err)
	}
	defer holder.Unlock()

	b := &ExecBridge{ConfigDir: dir, LockMaxWait: 150 * time.Millisecond}
	start := time.Now()
	release, failOpen := b.DeliverLock(context.Background())
	elapsed := time.Since(start)

	if release == nil {
		t.Fatal("DeliverLock returned a nil release (must always be usable)")
	}
	if !failOpen {
		t.Fatal("held past the bound: DeliverLock must report failOpen=true")
	}
	if elapsed < 100*time.Millisecond {
		t.Fatalf("DeliverLock returned too fast (%v) — did it wait out the bound?", elapsed)
	}
	if elapsed > 3*time.Second {
		t.Fatalf("DeliverLock blocked far past its bound (%v) — not fail-open", elapsed)
	}
	// The holder must still own the lock — fail-open does not steal it, so a fresh
	// non-blocking attempt must still see it held.
	if l, err := fslock.TryLock(lockPath); !errors.Is(err, fslock.ErrWouldBlock) {
		if l != nil {
			_ = l.Unlock()
		}
		t.Fatalf("holder lost the lock after DeliverLock fail-open: err=%v", err)
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
	release, failOpen := b.DeliverLock(context.Background())
	defer release()
	if failOpen {
		t.Fatal("uncontended acquire under ~/.claude: failOpen must be false")
	}

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
	release, failOpen := b.DeliverLock(context.Background())
	if release == nil {
		t.Fatal("release must never be nil")
	}
	if failOpen {
		t.Fatal("no lock configured is not a contention fail-open; failOpen must be false")
	}
	if err := release(); err != nil {
		t.Fatalf("no-op release err: %v", err)
	}
}
