//go:build unix

package command

import "syscall"

// FreeBytes reports the space available to an unprivileged writer (Bavail, not
// Bfree — the reserved blocks a self update cannot use).
func (OSSelfUpdateRunner) FreeBytes(dir string) (uint64, error) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(dir, &st); err != nil {
		return 0, err
	}
	return st.Bavail * uint64(st.Bsize), nil
}

// Exec replaces the running process image with the freshly installed binary,
// keeping argv and the environment. It returns only on failure.
func (OSSelfUpdateRunner) Exec(argv0 string, argv, envv []string) error {
	return syscall.Exec(argv0, argv, envv)
}
