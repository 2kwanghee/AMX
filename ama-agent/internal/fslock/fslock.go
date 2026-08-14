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
