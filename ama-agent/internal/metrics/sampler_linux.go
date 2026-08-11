//go:build linux

package metrics

import (
	"os"
	"syscall"
)

// Sampler collects host utilization on Linux. It is stateful only for CPU: the
// previous /proc/stat reading is kept so each Sample reports the busy fraction
// over the interval since the last call (the heartbeat period). NewSampler
// primes that baseline so the first heartbeat already carries a real delta.
type Sampler struct {
	prev     cpuTimes
	prevOK   bool
	diskPath string
}

// NewSampler returns a Sampler with the CPU baseline primed from /proc/stat.
// diskPath is the filesystem measured for DISK% (empty defaults to "/"); it lets
// a deployment point statfs at the real data volume instead of the root fs.
// A failed prime is non-fatal: the first Sample re-reads and seeds the baseline,
// reporting CPU from the second heartbeat onward.
func NewSampler(diskPath string) *Sampler {
	if diskPath == "" {
		diskPath = "/"
	}
	s := &Sampler{diskPath: diskPath}
	if data, err := os.ReadFile("/proc/stat"); err == nil {
		if ct, perr := parseStat(data); perr == nil {
			s.prev, s.prevOK = ct, true
		}
	}
	return s
}

// Sample reads CPU (delta vs the previous call), memory, and root-filesystem
// usage. It returns an error if any source is unreadable; the caller then omits
// the heartbeat metrics field rather than sending a partial/zeroed sample. CPU
// is 0 on the very first call if the baseline could not be primed.
func (s *Sampler) Sample() (Sample, error) {
	var out Sample

	statData, err := os.ReadFile("/proc/stat")
	if err != nil {
		return Sample{}, err
	}
	cur, err := parseStat(statData)
	if err != nil {
		return Sample{}, err
	}
	if s.prevOK {
		out.CPUPct = cpuPct(s.prev, cur)
	}
	s.prev, s.prevOK = cur, true

	memData, err := os.ReadFile("/proc/meminfo")
	if err != nil {
		return Sample{}, err
	}
	out.MemPct, err = parseMemInfo(memData)
	if err != nil {
		return Sample{}, err
	}

	var st syscall.Statfs_t
	if err := syscall.Statfs(s.diskPath, &st); err != nil {
		return Sample{}, err
	}
	out.DiskPct = diskPct(uint64(st.Blocks), uint64(st.Bfree), uint64(st.Bavail))

	return out, nil
}
