//go:build windows

package command

import (
	"os"
	"os/exec"
	"syscall"
	"unsafe"
)

// FreeBytes reports the space available to the caller on the volume holding dir,
// via GetDiskFreeSpaceExW (kernel32). The unix path uses statfs; Windows has no
// statfs, so this is the platform equivalent the self_update preflight needs. It
// is only reached in binary (package) mode — git mode targets Linux servers.
func (OSSelfUpdateRunner) FreeBytes(dir string) (uint64, error) {
	p, err := syscall.UTF16PtrFromString(dir)
	if err != nil {
		return 0, err
	}
	proc := syscall.NewLazyDLL("kernel32.dll").NewProc("GetDiskFreeSpaceExW")
	var freeToCaller uint64
	r, _, callErr := proc.Call(
		uintptr(unsafe.Pointer(p)),
		uintptr(unsafe.Pointer(&freeToCaller)),
		0, 0,
	)
	if r == 0 {
		return 0, callErr
	}
	return freeToCaller, nil
}

// Exec restarts into the freshly installed binary. Windows has no syscall.Exec
// and cannot overwrite a running .exe in place, so the swap (in swapAndRestart)
// first renames the running binary aside and moves the new one into place; here
// we launch the new binary detached with the same args/env and exit, letting it
// (or the service manager) take over. It returns only if the restart could not
// be launched.
func (OSSelfUpdateRunner) Exec(argv0 string, argv, envv []string) error {
	var rest []string
	if len(argv) > 1 {
		rest = argv[1:]
	}
	c := exec.Command(argv0, rest...)
	c.Env = envv
	c.Stdout = os.Stdout
	c.Stderr = os.Stderr
	if err := c.Start(); err != nil {
		return err
	}
	os.Exit(0)
	return nil // unreachable
}
