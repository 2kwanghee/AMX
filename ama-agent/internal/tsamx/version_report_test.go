package tsamx

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestParseVersionToken(t *testing.T) {
	cases := map[string]string{
		"tsamx 0.25.0b1": "0.25.0b1",
		"cswap 1.2.3\n":  "1.2.3",
		"":               "",
		"   \n":          "",
		"tsamx":          "tsamx",
		"tsamx  9.9.9  ": "9.9.9",
	}
	for raw, want := range cases {
		if got := parseVersionToken(raw); got != want {
			t.Errorf("parseVersionToken(%q) = %q, want %q", raw, got, want)
		}
	}
}

// TestQueryEngineVersionAbsent exercises the LookPath failure branch (P0-B item
// 2: binary not on PATH -> "tsamx/absent"), independent of whether a real tsamx
// happens to be installed on the machine running this test.
func TestQueryEngineVersionAbsent(t *testing.T) {
	got := queryEngineVersion(context.Background(), "amx-seat-engine-p0-nonexistent-binary")
	if got != "tsamx/absent" {
		t.Errorf("queryEngineVersion(missing binary) = %q, want tsamx/absent", got)
	}
}

// TestQueryEngineVersionExitFailure exercises the "found but fails" branch of
// the same absent outcome (P0-B item 2: "실행 실패"): a binary that exists and
// is executable but exits non-zero on --version.
func TestQueryEngineVersionExitFailure(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("shell script fixture is unix-only")
	}
	dir := t.TempDir()
	script := filepath.Join(dir, "failing-tsamx")
	if err := os.WriteFile(script, []byte("#!/bin/sh\nexit 1\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	got := queryEngineVersion(context.Background(), script)
	if got != "tsamx/absent" {
		t.Errorf("queryEngineVersion(failing binary) = %q, want tsamx/absent", got)
	}
}

// TestQueryEngineVersionUnparseable exercises the "ran fine, output made no
// sense" branch (P0-B item 1: version query failure -> "tsamx/unknown"): the
// binary exits 0 but prints nothing.
func TestQueryEngineVersionUnparseable(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("shell script fixture is unix-only")
	}
	dir := t.TempDir()
	script := filepath.Join(dir, "silent-tsamx")
	if err := os.WriteFile(script, []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	got := queryEngineVersion(context.Background(), script)
	if got != "tsamx/unknown" {
		t.Errorf("queryEngineVersion(silent binary) = %q, want tsamx/unknown", got)
	}
}

// TestQueryEngineVersionSuccess exercises the happy path against a fixture
// binary that mimics tsamx's actual `--version` output shape (argparse's
// "%(prog)s <version>", confirmed against the real installed tsamx).
func TestQueryEngineVersionSuccess(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("shell script fixture is unix-only")
	}
	dir := t.TempDir()
	script := filepath.Join(dir, "fake-tsamx")
	if err := os.WriteFile(script, []byte("#!/bin/sh\necho 'tsamx 0.25.0b1'\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	got := queryEngineVersion(context.Background(), script)
	if got != "tsamx/0.25.0b1" {
		t.Errorf("queryEngineVersion(fake tsamx) = %q, want tsamx/0.25.0b1", got)
	}
}

// TestEngineVersionCachesAcrossCalls confirms EngineVersion's process-lifetime
// memoization (design note P0-B: "등록마다 프로세스를 새로 띄우지 않도록 프로세스
// 수명 동안 1회 캐시"): a second call with a DIFFERENT binary must still return
// the first call's cached answer, because the query only ever runs once per
// process via sync.Once.
func TestEngineVersionCachesAcrossCalls(t *testing.T) {
	first := EngineVersion(context.Background(), "amx-seat-engine-p0-nonexistent-binary")
	second := EngineVersion(context.Background(), "some-other-binary-entirely")
	if first != second {
		t.Errorf("EngineVersion is not memoized: first=%q second=%q", first, second)
	}
}
