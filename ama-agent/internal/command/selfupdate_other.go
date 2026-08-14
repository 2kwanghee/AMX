//go:build !unix && !windows

package command

import "errors"

// errSelfUpdateUnsupported keeps the remaining (non-unix, non-windows) builds
// compiling. The agent targets Linux servers and Windows package installs; on
// any other platform the preflight fails and the running binary is never
// touched.
var errSelfUpdateUnsupported = errors.New("command: self_update is not supported on this platform")

func (OSSelfUpdateRunner) FreeBytes(string) (uint64, error) { return 0, errSelfUpdateUnsupported }

func (OSSelfUpdateRunner) Exec(string, []string, []string) error { return errSelfUpdateUnsupported }
