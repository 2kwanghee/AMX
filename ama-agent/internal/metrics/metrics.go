// Package metrics samples host CPU/MEM/DISK utilization for the heartbeat.
//
// Collection is best-effort and dependency-free: it reads /proc and statfs
// directly (no gopsutil) on Linux, and reports nothing on other platforms so
// the caller can omit the Heartbeat.metrics field entirely. The pure parsers
// below carry no build tag and no syscall, so they are unit-tested against
// fixed /proc fixtures on any platform; the OS-bound Sampler lives in the
// per-platform files.
package metrics

import (
	"errors"
	"strconv"
	"strings"
)

// ErrUnsupported is returned by Sample on platforms without /proc + statfs.
var ErrUnsupported = errors.New("metrics: host sampling unsupported on this platform")

// Sample is one host resource reading. Percentages are 0.0–100.0.
type Sample struct {
	CPUPct  float64
	MemPct  float64
	DiskPct float64
}

// cpuTimes holds the two aggregates needed for a CPU-utilization delta: total
// jiffies across all states and the idle portion (idle + iowait).
type cpuTimes struct {
	total uint64
	idle  uint64
}

// parseStat extracts the aggregate "cpu" line of /proc/stat. Fields after the
// label are (user, nice, system, idle, iowait, irq, softirq, steal, ...); total
// is their sum and idle counts the idle + iowait columns.
func parseStat(data []byte) (cpuTimes, error) {
	for _, line := range strings.Split(string(data), "\n") {
		if !strings.HasPrefix(line, "cpu ") {
			continue
		}
		fields := strings.Fields(line)[1:] // drop the "cpu" label
		var ct cpuTimes
		for i, f := range fields {
			v, err := strconv.ParseUint(f, 10, 64)
			if err != nil {
				return cpuTimes{}, err
			}
			// Columns are user,nice,system,idle,iowait,irq,softirq,steal,guest,
			// guest_nice. guest (idx 8) and guest_nice (idx 9) are already folded
			// into user and nice by the kernel, so summing them into total would
			// double-count and depress the busy fraction.
			if i >= 8 {
				continue
			}
			ct.total += v
			if i == 3 || i == 4 { // idle, iowait
				ct.idle += v
			}
		}
		if ct.total == 0 {
			return cpuTimes{}, errors.New("metrics: empty /proc/stat cpu line")
		}
		return ct, nil
	}
	return cpuTimes{}, errors.New("metrics: no cpu line in /proc/stat")
}

// cpuPct is the busy fraction between two /proc/stat samples. A zero or negative
// total delta (samples too close, or a counter reset) reports 0 rather than a
// spurious spike.
func cpuPct(prev, cur cpuTimes) float64 {
	totalDelta := int64(cur.total) - int64(prev.total)
	idleDelta := int64(cur.idle) - int64(prev.idle)
	if totalDelta <= 0 || idleDelta < 0 {
		return 0
	}
	busy := float64(totalDelta-idleDelta) / float64(totalDelta) * 100
	return clampPct(busy)
}

// parseMemInfo computes used-memory percent from /proc/meminfo as
// (MemTotal - MemAvailable) / MemTotal. MemAvailable is the kernel's estimate of
// memory obtainable for new work without swapping, so this ratio reflects memory
// pressure (in-use plus non-reclaimable) — close to, but not identical to,
// `free`'s "used" column. Values are in kB; the ratio is unit-independent.
func parseMemInfo(data []byte) (float64, error) {
	var total, available uint64
	var haveTotal, haveAvail bool
	for _, line := range strings.Split(string(data), "\n") {
		key, rest, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		switch key {
		case "MemTotal":
			total, haveTotal = parseKB(rest)
		case "MemAvailable":
			available, haveAvail = parseKB(rest)
		}
	}
	if !haveTotal || !haveAvail || total == 0 {
		return 0, errors.New("metrics: MemTotal/MemAvailable missing in /proc/meminfo")
	}
	if available > total {
		available = total
	}
	return clampPct(float64(total-available) / float64(total) * 100), nil
}

// parseKB reads the leading integer of a "  123456 kB" meminfo value.
func parseKB(rest string) (uint64, bool) {
	fields := strings.Fields(rest)
	if len(fields) == 0 {
		return 0, false
	}
	v, err := strconv.ParseUint(fields[0], 10, 64)
	if err != nil {
		return 0, false
	}
	return v, true
}

// diskPct is the used fraction of a filesystem, matching `df`: used = total -
// free blocks, and the denominator is used + blocks available to unprivileged
// users (so reserved blocks are excluded, as df does).
func diskPct(blocks, bfree, bavail uint64) float64 {
	if blocks == 0 {
		return 0
	}
	used := blocks - bfree
	denom := used + bavail
	if denom == 0 {
		return 0
	}
	return clampPct(float64(used) / float64(denom) * 100)
}

func clampPct(v float64) float64 {
	if v < 0 {
		return 0
	}
	if v > 100 {
		return 100
	}
	return v
}
