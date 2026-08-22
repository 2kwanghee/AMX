package autoswitch

import (
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func TestStatePath_DoesNotShareTsamxPath(t *testing.T) {
	dataHome := "/tmp/example-data-home"
	got := StatePath(dataHome)
	want := filepath.Join(dataHome, "ama-autoswitch", "state.json")
	if got != want {
		t.Fatalf("StatePath = %q, want %q", got, want)
	}
	// contract C6 names tsamx's own path as <dataHome>/tsamx/autoswitch_state.json
	// (internal/tsamx/exec.go's AutoStatePath/backupRoot) — this engine's path
	// must never collide with it.
	tsamxPath := filepath.Join(dataHome, "tsamx", "autoswitch_state.json")
	if got == tsamxPath {
		t.Fatalf("StatePath collided with tsamx's autoswitch_state.json path")
	}
}

func TestWriteState_ReadState_RoundTrip(t *testing.T) {
	dir := t.TempDir()
	path := StatePath(dir)
	at := time.Date(2026, 8, 23, 1, 2, 3, 0, time.UTC)
	in := map[string]QuarantineEntry{
		"deadbeef": {Email: "dead@x.io", Reason: "relogin_required", RefreshTokenFingerprint: "fp-1", At: at},
	}
	if err := WriteState(path, in); err != nil {
		t.Fatalf("WriteState: %v", err)
	}
	got, err := ReadState(path)
	if err != nil {
		t.Fatalf("ReadState: %v", err)
	}
	entry, ok := got["deadbeef"]
	if !ok || entry.Email != "dead@x.io" || entry.Reason != "relogin_required" || entry.RefreshTokenFingerprint != "fp-1" {
		t.Fatalf("round-trip = %+v, want the entry written", got)
	}
	if !entry.At.Equal(at) {
		t.Fatalf("At = %v, want %v", entry.At, at)
	}

	// no leftover state-*.tmp files after a successful atomic rename.
	matches, _ := filepath.Glob(filepath.Join(dir, "state-*.tmp"))
	if len(matches) != 0 {
		t.Fatalf("temp files still present after WriteState: %v", matches)
	}
}

func TestReadState_MissingFileIsEmpty(t *testing.T) {
	dir := t.TempDir()
	got, err := ReadState(StatePath(dir))
	if err != nil {
		t.Fatalf("ReadState: %v", err)
	}
	if len(got) != 0 {
		t.Fatalf("got = %+v, want empty map for a missing file", got)
	}
}

// TestReadState_CorruptFileReturnsError locks review C5's fix: a corrupt
// (unparseable) state file must surface as an error, NOT silently swallow
// to an empty map — the first version's swallow behavior meant a corrupted
// file silently released every quarantined account.
func TestReadState_CorruptFileReturnsError(t *testing.T) {
	dir := t.TempDir()
	path := StatePath(dir)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("{not valid json"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := ReadState(path)
	if err == nil {
		t.Fatalf("ReadState on a corrupt file returned nil error, want non-nil (review C5)")
	}
}

// TestWriteState_ConcurrentWritesDoNotCollide locks review C5's fix: the
// temp file name is now unique per call, so two concurrent WriteState
// calls to the same path cannot interleave their writes/renames the way a
// single fixed "state.json.tmp" name could.
func TestWriteState_ConcurrentWritesDoNotCollide(t *testing.T) {
	dir := t.TempDir()
	path := StatePath(dir)
	var wg sync.WaitGroup
	errs := make([]error, 10)
	for i := 0; i < 10; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			errs[i] = WriteState(path, map[string]QuarantineEntry{
				"key": {Email: "x@x.io", At: time.Now()},
			})
		}(i)
	}
	wg.Wait()
	for i, err := range errs {
		if err != nil {
			t.Fatalf("WriteState[%d]: %v", i, err)
		}
	}
	// The file must end up valid (some writer's complete content, not a
	// mangled interleave of two).
	got, err := ReadState(path)
	if err != nil {
		t.Fatalf("ReadState after concurrent writes: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("got = %+v, want exactly one entry", got)
	}
}

// --- ShouldRelease (review C1): fingerprint/account-replaced only --------

func TestShouldRelease_StatusAloneNeverReleases(t *testing.T) {
	entry := QuarantineEntry{Email: "dead@x.io", RefreshTokenFingerprint: "fp-1"}
	// Same email, no fingerprint supplied this tick ("" = not computed) ->
	// must NOT release, regardless of what usageStatus now says (this
	// function doesn't even take a status — that is the point of C1's fix).
	release, _ := ShouldRelease(entry, "dead@x.io", true, "")
	if release {
		t.Fatalf("ShouldRelease with unchanged email/no fingerprint = true, want false")
	}
	// Same email, SAME fingerprint supplied -> still no release.
	release, _ = ShouldRelease(entry, "dead@x.io", true, "fp-1")
	if release {
		t.Fatalf("ShouldRelease with unchanged fingerprint = true, want false")
	}
}

func TestShouldRelease_FingerprintChanged(t *testing.T) {
	entry := QuarantineEntry{Email: "dead@x.io", RefreshTokenFingerprint: "fp-1"}
	release, reason := ShouldRelease(entry, "dead@x.io", true, "fp-2")
	if !release || reason != "credentials-replaced" {
		t.Fatalf("release/reason = %v/%q, want true/credentials-replaced", release, reason)
	}
}

func TestShouldRelease_AccountReplacedOrRemoved(t *testing.T) {
	entry := QuarantineEntry{Email: "dead@x.io"}
	// different email at the same lookup -> account-replaced
	release, reason := ShouldRelease(entry, "someone-else@x.io", true, "")
	if !release || reason != "account-replaced" {
		t.Fatalf("release/reason = %v/%q, want true/account-replaced", release, reason)
	}
	// account removed entirely (present=false) -> also account-replaced
	release, reason = ShouldRelease(entry, "", false, "")
	if !release || reason != "account-replaced" {
		t.Fatalf("release/reason = %v/%q, want true/account-replaced", release, reason)
	}
}
