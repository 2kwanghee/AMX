//go:build !linux

package metrics

// Sampler is a no-op on non-Linux hosts: there is no portable /proc + statfs
// path, so Sample always fails and the caller omits the heartbeat metrics field.
type Sampler struct{}

// NewSampler returns a Sampler that reports nothing. diskPath is accepted for
// signature parity with the Linux build and ignored.
func NewSampler(diskPath string) *Sampler { return &Sampler{} }

// Sample always returns ErrUnsupported off Linux.
func (s *Sampler) Sample() (Sample, error) { return Sample{}, ErrUnsupported }
