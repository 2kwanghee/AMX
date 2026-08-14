// Package fslock provides a small cross-platform advisory file lock that
// captures exactly the pattern the deliver critical section needs: a
// NON-BLOCKING attempt at an exclusive lock, with a distinguishable
// "already held by someone else" result, plus a release that unlocks and
// closes the underlying file.
//
// The retry/deadline/fail-open policy stays in the callers (the deliver
// bridges) so each can keep its own bound; this package is only the
// primitive. On unix it is backed by flock(LOCK_EX|LOCK_NB); on Windows by
// LockFileEx(LOCKFILE_EXCLUSIVE_LOCK|LOCKFILE_FAIL_IMMEDIATELY). Both yield
// the same lock semantics: one holder at a time, released on Unlock or when
// the process exits.
//
// Contract — the callers rely on all of these:
//
//   - EMPTY SENTINEL FILES ONLY. Lock a dedicated zero-byte lock file, never a
//     real data file. On unix flock is advisory (nothing enforces it against a
//     writer), but on Windows LockFileEx is a MANDATORY byte-range lock, so
//     locking a data file would block real reads/writes there and split the
//     behaviour across platforms. Keeping the lock on a throwaway sentinel makes
//     the two platforms behave alike.
//   - Unlock EXACTLY ONCE. It is NOT idempotent: it unlocks and closes the file,
//     so a second call operates on a closed handle. Each successful TryLock is
//     paired with one Unlock.
//   - Windows contention may surface earlier, at the OpenFile step, as
//     ERROR_SHARING_VIOLATION rather than as ErrWouldBlock from the lock call —
//     so a contended TryLock can return a plain error instead of ErrWouldBlock.
//     The deliver callers fail-open on any non-ErrWouldBlock error, so this is
//     safe there; a caller that must distinguish "held" from "broken" on Windows
//     cannot rely on ErrWouldBlock alone.
//
// WINDOWS DELIVER-LOCK ASYMMETRY (B1b): this package only ever locks the AGENT
// side. The runner-side guard lives in the shell wrappers deploy/amx-claude and
// deploy/amx-codex, which take their shared lock via flock(1). Git Bash ships no
// flock(1), so on Windows native those wrappers silently fall open and launch the
// runner unlocked. The net effect: on Windows only the agent's LockFileEx lock
// exists, the runner never blocks on it, and the B1b defense (stop a runner from
// starting mid-deliver) is INCOMPLETE. B1a (previous-active restore + atomic
// write) still bounds the residual exposure to the sub-second swap window.
package fslock

import (
	"errors"
	"os"
)

// ErrWouldBlock reports that the lock is currently held by another holder, so
// the non-blocking acquisition would have blocked. Callers treat this as
// "someone else has it" (retry within a bound, then fail-open), distinct from
// an unexpected error where they fail-open immediately.
var ErrWouldBlock = errors.New("fslock: lock would block")

// Lock is an acquired exclusive advisory lock over a file.
type Lock struct {
	f *os.File
}
