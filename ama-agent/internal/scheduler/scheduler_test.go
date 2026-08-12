package scheduler

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
	"github.com/2kwanghee/AMX/ama-agent/internal/reporter"
	"github.com/2kwanghee/AMX/ama-agent/internal/tsamx"
	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
)

const testAgent = "ama_test"

// seed builds a Fake pool with the given emails, activating the first, and
// returns a Scheduler + Outbox wired over it.
func seed(t *testing.T, interval time.Duration, emails ...string) (*tsamx.Fake, *Scheduler, *reporter.Outbox) {
	t.Helper()
	f := tsamx.NewFake()
	ctx := context.Background()
	for _, e := range emails {
		if err := f.Add(ctx, provider.AddRequest{Email: e, Enable: true}); err != nil {
			t.Fatal(err)
		}
	}
	// Fake.Add now activates each added slot (models real tsamx add); seed the
	// intended active account (the first) after all adds.
	if len(emails) > 0 {
		f.SetActiveEmail(emails[0])
	}
	ob := reporter.NewOutbox()
	s := New(Config{
		AgentID:  testAgent,
		Bridge:   f,
		Reporter: reporter.New(testAgent, map[string]provider.Bridge{provider.DefaultProvider: f}, time.Now),
		Outbox:   ob,
		Interval: interval,
	})
	return f, s, ob
}

// drain flushes the outbox into a slice for assertions.
func drain(t *testing.T, ob *reporter.Outbox) []*amxv1.AccountEvent {
	t.Helper()
	var got []*amxv1.AccountEvent
	if err := ob.Flush(func(ev *amxv1.AccountEvent) error {
		got = append(got, ev)
		return nil
	}); err != nil {
		t.Fatalf("flush: %v", err)
	}
	return got
}

func TestTickActiveChangeEnqueuesSwitch(t *testing.T) {
	f, s, ob := seed(t, time.Hour, "a@x.io", "b@x.io")
	// Model a switch: auto --once moves the active account and reports code 0.
	f.AutoFn = func(fk *tsamx.Fake) int {
		fk.SetActiveEmail("b@x.io")
		return 0
	}
	s.Tick(context.Background())

	got := drain(t, ob)
	if len(got) != 1 {
		t.Fatalf("events = %d, want 1", len(got))
	}
	ev := got[0]
	if ev.GetKind() != amxv1.AccountEvent_KIND_SWITCH {
		t.Fatalf("kind = %v, want SWITCH", ev.GetKind())
	}
	if ev.GetTrigger() != amxv1.AccountEvent_TRIGGER_AT_LIMIT {
		t.Fatalf("trigger = %v, want AT_LIMIT", ev.GetTrigger())
	}
	if ev.GetFrom().GetEmail() != "a@x.io" || ev.GetTo().GetEmail() != "b@x.io" {
		t.Fatalf("from/to = %q/%q, want a@x.io/b@x.io", ev.GetFrom().GetEmail(), ev.GetTo().GetEmail())
	}
	if ev.GetEventId() == "" {
		t.Fatal("event_id empty")
	}
}

func TestTickNoActionNoEvent(t *testing.T) {
	f, s, ob := seed(t, time.Hour, "a@x.io", "b@x.io")
	f.AutoCode = 2 // no action; active unchanged
	s.Tick(context.Background())
	if got := drain(t, ob); len(got) != 0 {
		t.Fatalf("events = %d, want 0", len(got))
	}
}

func TestTickAllExhaustedByCode(t *testing.T) {
	f, s, ob := seed(t, time.Hour, "a@x.io", "b@x.io")
	f.AutoCode = 3 // blocked: wanted to switch but all exhausted
	s.Tick(context.Background())
	got := drain(t, ob)
	if len(got) != 1 {
		t.Fatalf("events = %d, want 1", len(got))
	}
	if got[0].GetKind() != amxv1.AccountEvent_KIND_ALL_EXHAUSTED {
		t.Fatalf("kind = %v, want ALL_EXHAUSTED", got[0].GetKind())
	}
}

func TestTickAllExhaustedByPoolSummary(t *testing.T) {
	f, s, ob := seed(t, time.Hour, "a@x.io", "b@x.io")
	// Every enabled account at/over the 95% threshold -> pool.all_exhausted.
	f.SetUsage("a@x.io", &provider.Usage{FiveHour: &provider.Window{Pct: 99}})
	f.SetUsage("b@x.io", &provider.Usage{FiveHour: &provider.Window{Pct: 97}})
	f.AutoCode = 2 // no switch happened, but the pool is exhausted
	s.Tick(context.Background())
	got := drain(t, ob)
	if len(got) != 1 || got[0].GetKind() != amxv1.AccountEvent_KIND_ALL_EXHAUSTED {
		t.Fatalf("events = %+v, want 1 ALL_EXHAUSTED", got)
	}
}

func TestStartTicksAndStopHalts(t *testing.T) {
	f, s, _ := seed(t, 5*time.Millisecond, "a@x.io", "b@x.io")
	f.AutoCode = 2

	s.Start()
	if !s.Running() {
		t.Fatal("scheduler not running after Start")
	}
	// Wait for at least one tick.
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if countAuto(f.CallLog()) >= 1 {
			break
		}
		time.Sleep(2 * time.Millisecond)
	}
	if countAuto(f.CallLog()) < 1 {
		t.Fatal("no tick observed while running")
	}

	s.Stop()
	if s.Running() {
		t.Fatal("scheduler still running after Stop")
	}
	after := countAuto(f.CallLog())
	time.Sleep(30 * time.Millisecond)
	if got := countAuto(f.CallLog()); got != after {
		t.Fatalf("ticks continued after Stop: %d -> %d", after, got)
	}
}

func TestManualModeNeverTicks(t *testing.T) {
	// A scheduler that is never Started (manual mode) must not touch tsamx.
	f, _, _ := seed(t, 5*time.Millisecond, "a@x.io", "b@x.io")
	time.Sleep(30 * time.Millisecond)
	if got := countAuto(f.CallLog()); got != 0 {
		t.Fatalf("auto --once called %d times without Start", got)
	}
}

func countAuto(log []string) int {
	n := 0
	for _, c := range log {
		if strings.HasPrefix(c, "auto --once") {
			n++
		}
	}
	return n
}
