//go:build unix

package fslock

import (
	"errors"
	"os"
	"syscall"
)

// TryLock opens path (creating it 0600 if absent) and attempts a non-blocking
// exclusive flock. It returns (lock, nil) on success, (nil, ErrWouldBlock) when
// another holder currently holds it, or (nil, err) on any other failure. On
// failure the file it opened is always closed, so a failed attempt leaks
// nothing.
func TryLock(path string) (*Lock, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = f.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return nil, ErrWouldBlock
		}
		return nil, err
	}
	return &Lock{f: f}, nil
}

// Unlock releases the flock and closes the underlying file. The unlock error
// (if any) wins over the close error, matching the prior inline behaviour.
func (l *Lock) Unlock() error {
	unlockErr := syscall.Flock(int(l.f.Fd()), syscall.LOCK_UN)
	closeErr := l.f.Close()
	if unlockErr != nil {
		return unlockErr
	}
	return closeErr
}
