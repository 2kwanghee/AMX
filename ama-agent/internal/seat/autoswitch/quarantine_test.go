package autoswitch

import (
	"os"
	"path/filepath"
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
		"2": {Email: "dead@x.io", Reason: "relogin_required", At: at},
	}
	if err := WriteState(path, in); err != nil {
		t.Fatalf("WriteState: %v", err)
	}
	got, err := ReadState(path)
	if err != nil {
		t.Fatalf("ReadState: %v", err)
	}
	if len(got) != 1 || got["2"].Email != "dead@x.io" || got["2"].Reason != "relogin_required" {
		t.Fatalf("round-trip = %+v, want the entry written", got)
	}
	if !got["2"].At.Equal(at) {
		t.Fatalf("At = %v, want %v", got["2"].At, at)
	}

	// no tmp file left behind after a successful atomic rename.
	if _, err := os.Stat(path + ".tmp"); !os.IsNotExist(err) {
		t.Fatalf("temp file still present after WriteState: err=%v", err)
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

func TestShouldRelease(t *testing.T) {
	if ShouldRelease("relogin_required", true) {
		t.Fatalf("still relogin_required must not release")
	}
	if !ShouldRelease("ok", true) {
		t.Fatalf("status recovered while quarantined must release")
	}
	if ShouldRelease("ok", false) {
		t.Fatalf("not currently quarantined -> nothing to release")
	}
}
