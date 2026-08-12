//go:build !unix

package command

import "errors"

// errSelfUpdateUnsupported keeps non-unix builds compiling. The agent targets
// Linux servers; on any other platform the preflight fails and the running
// binary is never touched.
var errSelfUpdateUnsupported = errors.New("command: self_update is not supported on this platform")

func (OSSelfUpdateRunner) FreeBytes(string) (uint64, error) { return 0, errSelfUpdateUnsupported }

func (OSSelfUpdateRunner) Exec(string, []string, []string) error { return errSelfUpdateUnsupported }
