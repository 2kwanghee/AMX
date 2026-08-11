package metrics

import (
	"math"
	"testing"
)

func approx(a, b float64) bool { return math.Abs(a-b) < 1e-6 }

func TestParseMemInfo(t *testing.T) {
	// MemTotal 8000 kB, MemAvailable 2000 kB -> 75% used.
	data := []byte("MemTotal:        8000 kB\nMemFree:          500 kB\nMemAvailable:    2000 kB\nBuffers:          100 kB\n")
	got, err := parseMemInfo(data)
	if err != nil {
		t.Fatal(err)
	}
	if !approx(got, 75) {
		t.Fatalf("mem pct = %v, want 75", got)
	}
}

func TestParseMemInfoMissing(t *testing.T) {
	if _, err := parseMemInfo([]byte("MemFree: 500 kB\n")); err == nil {
		t.Fatal("expected error when MemTotal/MemAvailable absent")
	}
}

func TestParseStatAndCPUPct(t *testing.T) {
	// user nice system idle iowait irq softirq steal
	prevData := []byte("cpu  100 0 100 700 100 0 0 0\ncpu0 50 0 50 350 50 0 0 0\n")
	curData := []byte("cpu  300 0 300 1300 100 0 0 0\n")
	prev, err := parseStat(prevData)
	if err != nil {
		t.Fatal(err)
	}
	cur, err := parseStat(curData)
	if err != nil {
		t.Fatal(err)
	}
	// prev total = 1000, idle(idle+iowait)=800. cur total = 2000, idle = 1400.
	// totalDelta=1000, idleDelta=600 -> busy = 400/1000 = 40%.
	if got := cpuPct(prev, cur); !approx(got, 40) {
		t.Fatalf("cpu pct = %v, want 40", got)
	}
}

func TestParseStatExcludesGuest(t *testing.T) {
	// Full 10-column kernel line: user nice system idle iowait irq softirq steal
	// guest guest_nice. guest(500) and guest_nice(500) are already folded into
	// user/nice, so total must exclude them: 100+100+100+700+100+0+0+0 = 1100,
	// idle(idle+iowait) = 800.
	ct, err := parseStat([]byte("cpu  100 100 100 700 100 0 0 0 500 500\n"))
	if err != nil {
		t.Fatal(err)
	}
	if ct.total != 1100 {
		t.Fatalf("total = %d, want 1100 (guest/guest_nice must be excluded)", ct.total)
	}
	if ct.idle != 800 {
		t.Fatalf("idle = %d, want 800", ct.idle)
	}
}

func TestCPUPctGuards(t *testing.T) {
	base := cpuTimes{total: 1000, idle: 800}
	// Non-advancing / regressing total delta reports 0, not a spurious value.
	if got := cpuPct(base, base); got != 0 {
		t.Fatalf("equal samples cpu pct = %v, want 0", got)
	}
	if got := cpuPct(cpuTimes{total: 2000, idle: 1000}, base); got != 0 {
		t.Fatalf("regressing samples cpu pct = %v, want 0", got)
	}
}

func TestDiskPct(t *testing.T) {
	// blocks 1000, bfree 300, bavail 200: used=700, denom=used+bavail=900 -> 77.77%.
	if got := diskPct(1000, 300, 200); !approx(got, 700.0/900.0*100) {
		t.Fatalf("disk pct = %v", got)
	}
	if got := diskPct(0, 0, 0); got != 0 {
		t.Fatalf("zero blocks disk pct = %v, want 0", got)
	}
}
