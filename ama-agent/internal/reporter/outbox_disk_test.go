package reporter

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
)

func mustOpen(t *testing.T, dir string) *Outbox {
	t.Helper()
	o, err := OpenOutbox(dir)
	if err != nil {
		t.Fatalf("OpenOutbox: %v", err)
	}
	return o
}

// drainAll flushes every queued event into a slice (send always succeeds).
func drainAll(t *testing.T, o *Outbox) []*amxv1.AccountEvent {
	t.Helper()
	var got []*amxv1.AccountEvent
	if err := o.Flush(func(ev *amxv1.AccountEvent) error {
		got = append(got, ev)
		return nil
	}); err != nil {
		t.Fatalf("Flush: %v", err)
	}
	return got
}

// TestDiskOutboxSurvivesRestart is C1 W1: events queued before a restart reload
// from disk and are still deliverable.
func TestDiskOutboxSurvivesRestart(t *testing.T) {
	dir := t.TempDir()

	o1 := mustOpen(t, dir)
	if err := o1.Enqueue(&amxv1.AccountEvent{EventId: "e1", Kind: amxv1.AccountEvent_KIND_SWITCH}); err != nil {
		t.Fatalf("enqueue e1: %v", err)
	}
	if err := o1.Enqueue(&amxv1.AccountEvent{EventId: "e2", Kind: amxv1.AccountEvent_KIND_ALL_EXHAUSTED}); err != nil {
		t.Fatalf("enqueue e2: %v", err)
	}

	// Restart: a brand-new Outbox over the same directory (o1 is abandoned, as a
	// crashed process would be).
	o2 := mustOpen(t, dir)
	if o2.Depth() != 2 {
		t.Fatalf("after restart depth = %d, want 2", o2.Depth())
	}
	got := drainAll(t, o2)
	if len(got) != 2 || got[0].GetEventId() != "e1" || got[1].GetEventId() != "e2" {
		t.Fatalf("reloaded events = %+v, want [e1 e2] in order", got)
	}
	// Kinds survive the proto round-trip.
	if got[0].GetKind() != amxv1.AccountEvent_KIND_SWITCH {
		t.Fatalf("e1 kind = %v, want SWITCH", got[0].GetKind())
	}
}

// TestDiskOutboxKeepsOnSendFailure is C1 W2: a send failure (stream drop) must
// not delete the event; it must persist across a restart and remain deliverable.
func TestDiskOutboxKeepsOnSendFailure(t *testing.T) {
	dir := t.TempDir()

	o1 := mustOpen(t, dir)
	_ = o1.Enqueue(&amxv1.AccountEvent{EventId: "keep1"})
	_ = o1.Enqueue(&amxv1.AccountEvent{EventId: "keep2"})

	// Flush where the send fails on the first event: nothing is deleted.
	sendErr := errors.New("stream drop")
	if err := o1.Flush(func(ev *amxv1.AccountEvent) error { return sendErr }); !errors.Is(err, sendErr) {
		t.Fatalf("Flush error = %v, want %v", err, sendErr)
	}
	if o1.Depth() != 2 {
		t.Fatalf("depth after failed flush = %d, want 2 (nothing deleted)", o1.Depth())
	}

	// Survives a restart and is still deliverable.
	o2 := mustOpen(t, dir)
	if got := drainAll(t, o2); len(got) != 2 {
		t.Fatalf("after restart, deliverable = %d, want 2", len(got))
	}
}

// TestDiskOutboxExactlyOnce: a confirmed (successful) send deletes the event, and
// a restart afterwards resurrects nothing — delivered exactly once.
func TestDiskOutboxExactlyOnce(t *testing.T) {
	dir := t.TempDir()

	o1 := mustOpen(t, dir)
	_ = o1.Enqueue(&amxv1.AccountEvent{EventId: "once"})
	if got := drainAll(t, o1); len(got) != 1 {
		t.Fatalf("first flush delivered %d, want 1", len(got))
	}
	if o1.Depth() != 0 {
		t.Fatalf("depth after successful flush = %d, want 0", o1.Depth())
	}

	// Restart: the delivered event's tombstone means it does not come back.
	o2 := mustOpen(t, dir)
	if o2.Depth() != 0 {
		t.Fatalf("after restart depth = %d, want 0 (no re-delivery)", o2.Depth())
	}
	if got := drainAll(t, o2); len(got) != 0 {
		t.Fatalf("restart re-delivered %d events, want 0", len(got))
	}
}

// TestDiskOutboxSkipsCorruptTail: a torn trailing line from a crash mid-append is
// skipped on load; the intact records still recover.
func TestDiskOutboxSkipsCorruptTail(t *testing.T) {
	dir := t.TempDir()

	o1 := mustOpen(t, dir)
	_ = o1.Enqueue(&amxv1.AccountEvent{EventId: "good1"})
	_ = o1.Enqueue(&amxv1.AccountEvent{EventId: "good2"})

	// Append a truncated JSON line, as an interrupted write would leave behind.
	path := filepath.Join(dir, OutboxLogFileName)
	f, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		t.Fatalf("open for corrupt append: %v", err)
	}
	if _, err := f.WriteString(`{"op":"add","seq":99,"ev":"dGhpcyBpcw`); err != nil {
		t.Fatalf("write corrupt tail: %v", err)
	}
	_ = f.Close()

	o2 := mustOpen(t, dir)
	got := drainAll(t, o2)
	if len(got) != 2 || got[0].GetEventId() != "good1" || got[1].GetEventId() != "good2" {
		t.Fatalf("after corrupt tail, recovered = %+v, want [good1 good2]", got)
	}
}

// TestDiskOutboxCompactionConcurrentEnqueueNoLoss is the F1 regression, made
// deterministic with compactMidHook: while a compaction is between rewriting the
// log (old inode unlinked) and swapping the append fd, a concurrent Enqueue fires.
// If compaction is not serialized with Enqueue under o.mu, that Enqueue writes to
// the dead inode and the event vanishes on reload. The fix holds o.mu across the
// whole compaction, so the concurrent Enqueue blocks until the new fd is in place
// and its write lands in the live file. Run with -race.
func TestDiskOutboxCompactionConcurrentEnqueueNoLoss(t *testing.T) {
	// Lower the threshold so a single delivered filler triggers compaction.
	savedThresh := outboxCompactThreshold
	outboxCompactThreshold = 1
	defer func() { outboxCompactThreshold = savedThresh }()

	dir := t.TempDir()
	o := mustOpen(t, dir)

	// filler is enqueued first (lowest seq) so Flush delivers it — triggering
	// compaction — before it reaches the never-delivered survivor and stops.
	if err := o.Enqueue(&amxv1.AccountEvent{EventId: "filler"}); err != nil {
		t.Fatalf("enqueue filler: %v", err)
	}
	if err := o.Enqueue(&amxv1.AccountEvent{EventId: "survivor"}); err != nil {
		t.Fatalf("enqueue survivor: %v", err)
	}

	// The hook races an Enqueue into the compaction window. In another goroutine so
	// the fixed code (which holds o.mu during compact) makes it block on o.mu; the
	// sleep gives it time to reach — and, if unsynchronized, complete — the write.
	var savedHook = compactMidHook
	enqueued := make(chan struct{})
	compactMidHook = func() {
		go func() {
			_ = o.Enqueue(&amxv1.AccountEvent{EventId: "midkeep"})
			close(enqueued)
		}()
		time.Sleep(50 * time.Millisecond)
	}
	defer func() { compactMidHook = savedHook }()

	// Deliver only the filler; survivor stays. This triggers exactly one compaction.
	if err := o.Flush(func(ev *amxv1.AccountEvent) error {
		if ev.GetEventId() == "filler" {
			return nil
		}
		return errors.New("hold non-filler")
	}); err == nil {
		t.Fatal("expected Flush to stop at the held survivor")
	}

	// Let the racing Enqueue finish (it lands after the fd swap in the fixed code).
	select {
	case <-enqueued:
	case <-time.After(2 * time.Second):
		t.Fatal("racing Enqueue never completed")
	}
	compactMidHook = savedHook

	// Reload: both the survivor and the mid-compaction enqueue must be on disk.
	o2 := mustOpen(t, dir)
	got := drainAll(t, o2)
	seen := make(map[string]struct{}, len(got))
	for _, ev := range got {
		seen[ev.GetEventId()] = struct{}{}
	}
	if _, ok := seen["survivor"]; !ok {
		t.Fatal("survivor lost across compaction")
	}
	if _, ok := seen["midkeep"]; !ok {
		t.Fatal("event enqueued during the compaction window was lost (F1)")
	}
	if _, ok := seen["filler"]; ok {
		t.Fatal("delivered filler resurrected on reload")
	}
}

// TestDiskOutboxDedupeAcrossRestart: the dedupe window is reseeded from reloaded
// events so a re-enqueue of a still-pending event_id is suppressed.
func TestDiskOutboxDedupeAcrossRestart(t *testing.T) {
	dir := t.TempDir()

	o1 := mustOpen(t, dir)
	_ = o1.Enqueue(&amxv1.AccountEvent{EventId: "dup"})

	o2 := mustOpen(t, dir)
	_ = o2.Enqueue(&amxv1.AccountEvent{EventId: "dup"}) // same id, still pending
	if o2.Depth() != 1 {
		t.Fatalf("depth after re-enqueue of pending id = %d, want 1 (deduped)", o2.Depth())
	}
}

// TestDiskOutboxCompaction: after enough delete tombstones the log is rewritten
// so it does not grow without bound, and the live set is preserved intact.
func TestDiskOutboxCompaction(t *testing.T) {
	dir := t.TempDir()
	o := mustOpen(t, dir)

	// Churn many events through enqueue+successful-flush to pile up delete
	// tombstones past the compaction threshold, which rewrites the log.
	for i := 0; i < outboxCompactThreshold+10; i++ {
		id := fmt.Sprintf("churn-%d", i)
		_ = o.Enqueue(&amxv1.AccountEvent{EventId: id})
		if got := drainAll(t, o); len(got) != 1 {
			t.Fatalf("iteration %d delivered %d, want 1", i, len(got))
		}
	}
	// One event enqueued after the churn must persist across a restart.
	_ = o.Enqueue(&amxv1.AccountEvent{EventId: "survivor"})

	// The on-disk log must have been compacted (far smaller than one line per op).
	info, err := os.Stat(filepath.Join(dir, OutboxLogFileName))
	if err != nil {
		t.Fatalf("stat log: %v", err)
	}
	if info.Size() > 64*1024 {
		t.Fatalf("log did not compact: size = %d bytes", info.Size())
	}

	// The survivor still reloads after a restart.
	o2 := mustOpen(t, dir)
	got := drainAll(t, o2)
	if len(got) != 1 || got[0].GetEventId() != "survivor" {
		t.Fatalf("after compaction+restart, live = %+v, want [survivor]", got)
	}
}
