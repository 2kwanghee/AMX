//go:build linux

package metrics

import "testing"

// TestSamplerDiskPathOverride covers the AMX_METRICS_DISK_PATH plumbing: the
// default resolves to "/", a valid path samples cleanly, and a bogus path makes
// statfs fail so Sample errors and the caller omits the heartbeat metrics field.
func TestSamplerDiskPathOverride(t *testing.T) {
	if s := NewSampler(""); s.diskPath != "/" {
		t.Fatalf("empty diskPath defaulted to %q, want \"/\"", s.diskPath)
	}
	if s := NewSampler("/tmp"); s.diskPath != "/tmp" {
		t.Fatalf("diskPath = %q, want /tmp", s.diskPath)
	}
	if _, err := NewSampler("/").Sample(); err != nil {
		t.Fatalf("sample with valid path: %v", err)
	}
	if _, err := NewSampler("/no/such/path/xyzzy-amx").Sample(); err == nil {
		t.Fatal("expected statfs error for a nonexistent disk path")
	}
}
