//go:build windows

package fslock

import (
	"errors"
	"os"

	"golang.org/x/sys/windows"
)

// TryLock opens path (creating it 0600 if absent) and attempts a non-blocking
// exclusive byte-range lock via LockFileEx. LOCKFILE_FAIL_IMMEDIATELY makes an
// already-locked region return ERROR_LOCK_VIOLATION instead of blocking, which
// maps to ErrWouldBlock so callers see the same "held by someone else" signal
// as EWOULDBLOCK on unix. On failure the opened file is always closed.
func TryLock(path string) (*Lock, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	// Lock a single byte at offset 0. The whole-file region is unnecessary; a
	// fixed one-byte region is enough for a whole-file advisory lock as long as
	// every holder locks the same region, which they do.
	err = windows.LockFileEx(
		windows.Handle(f.Fd()),
		windows.LOCKFILE_EXCLUSIVE_LOCK|windows.LOCKFILE_FAIL_IMMEDIATELY,
		0, 1, 0, new(windows.Overlapped),
	)
	if err != nil {
		_ = f.Close()
		if errors.Is(err, windows.ERROR_LOCK_VIOLATION) {
			return nil, ErrWouldBlock
		}
		return nil, err
	}
	return &Lock{f: f}, nil
}

// Unlock releases the byte-range lock and closes the underlying file. The
// unlock error (if any) wins over the close error.
func (l *Lock) Unlock() error {
	unlockErr := windows.UnlockFileEx(windows.Handle(l.f.Fd()), 0, 1, 0, new(windows.Overlapped))
	closeErr := l.f.Close()
	if unlockErr != nil {
		return unlockErr
	}
	return closeErr
}
