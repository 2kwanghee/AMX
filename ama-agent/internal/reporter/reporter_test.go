package reporter

import (
	"context"
	"fmt"
	"testing"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/tsamx"
	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
)

func TestBuildUsageReport(t *testing.T) {
	f := tsamx.NewFake()
	ctx := context.Background()
	_ = f.Add(ctx, tsamx.AddRequest{Email: "a@x.io", Enable: true})
	_ = f.Add(ctx, tsamx.AddRequest{Email: "b@x.io", Enable: true})
	_ = f.Switch(ctx, "a@x.io")
	f.SetUsage("a@x.io", &tsamx.Usage{FiveHour: &tsamx.Window{Pct: 61.2}, SevenDay: &tsamx.Window{Pct: 44}})
	f.SetUsage("b@x.io", &tsamx.Usage{FiveHour: &tsamx.Window{Pct: 12}})

	r := New("ama_test", f, func() time.Time { return time.Unix(1700000000, 0) })
	rep, err := r.BuildUsageReport(ctx, amxv1.UsageReport_TRIGGER_SCHEDULE)
	if err != nil {
		t.Fatal(err)
	}
	if rep.PoolSummary.Total != 2 {
		t.Fatalf("total = %d", rep.PoolSummary.Total)
	}
	if rep.PoolSummary.Eligible != 2 {
		t.Fatalf("eligible = %d, want 2", rep.PoolSummary.Eligible)
	}
	if rep.PoolSummary.AllExhausted {
		t.Fatal("allExhausted should be false")
	}
	if got := rep.PoolSummary.MaxUtilizationPct; got != 61.2 {
		t.Fatalf("maxUtilizationPct = %v, want 61.2", got)
	}
	if rep.ActiveAccount == nil || rep.ActiveAccount.Email != "a@x.io" {
		t.Fatalf("active account = %+v", rep.ActiveAccount)
	}
}

// TestBuildUsageReportStampsAmsAccountID (B1b review item 5): with an ID resolver
// installed, every account KNOWN to the manifest is stamped with its
// ams_account_id (and UUID); an account the resolver does not know (never
// assigned by AMS) stays email-only. AMS reconcile-on-report keys drift on
// ams_account_id, so a missing stamp reads as "absent" and triggers the redeliver
// loop that clobbers O9 rotations.
func TestBuildUsageReportStampsAmsAccountID(t *testing.T) {
	f := tsamx.NewFake()
	ctx := context.Background()
	_ = f.Add(ctx, tsamx.AddRequest{Email: "known@x.io", Enable: true})
	_ = f.Add(ctx, tsamx.AddRequest{Email: "stranger@x.io", Enable: true})

	r := New("ama_test", f, func() time.Time { return time.Unix(1700000000, 0) })
	// Resolver models the manifest: only "known@x.io" is an AMS-assigned account.
	r.SetIDResolver(func(email string) (string, string, bool) {
		if email == "known@x.io" {
			return "acc-known", "uuid-known", true
		}
		return "", "", false
	})

	rep, err := r.BuildUsageReport(ctx, amxv1.UsageReport_TRIGGER_SCHEDULE)
	if err != nil {
		t.Fatal(err)
	}
	byEmail := map[string]*amxv1.AccountRef{}
	for _, au := range rep.GetAccounts() {
		byEmail[au.GetAccount().GetEmail()] = au.GetAccount()
	}
	known := byEmail["known@x.io"]
	if known == nil || known.GetAmsAccountId() != "acc-known" || known.GetAccountUuid() != "uuid-known" {
		t.Fatalf("manifest account not stamped: %+v", known)
	}
	stranger := byEmail["stranger@x.io"]
	if stranger == nil || stranger.GetAmsAccountId() != "" || stranger.GetAccountUuid() != "" {
		t.Fatalf("unassigned account must stay email-only, got %+v", stranger)
	}
}

// TestBuildUsageReportNoResolverEmailOnly: without a resolver (the default), no
// account is stamped — reports remain email-only, unchanged from before f45508b.
func TestBuildUsageReportNoResolverEmailOnly(t *testing.T) {
	f := tsamx.NewFake()
	ctx := context.Background()
	_ = f.Add(ctx, tsamx.AddRequest{Email: "a@x.io", Enable: true})

	r := New("ama_test", f, func() time.Time { return time.Unix(1700000000, 0) })
	rep, err := r.BuildUsageReport(ctx, amxv1.UsageReport_TRIGGER_SCHEDULE)
	if err != nil {
		t.Fatal(err)
	}
	if len(rep.GetAccounts()) != 1 || rep.GetAccounts()[0].GetAccount().GetAmsAccountId() != "" {
		t.Fatalf("expected email-only account without a resolver, got %+v", rep.GetAccounts())
	}
}

// TestBuildUsageReportWindows (P2b): accountUsage dual-records the positional
// windows into the generalized windows[] list, ordered by window_minutes
// ascending, and omits a nil source window. maxUtilizationPct stays the max of
// the present windows (numeric equivalence with the former max(5h, 7d)).
func TestBuildUsageReportWindows(t *testing.T) {
	f := tsamx.NewFake()
	ctx := context.Background()
	_ = f.Add(ctx, tsamx.AddRequest{Email: "a@x.io", Enable: true})
	_ = f.Add(ctx, tsamx.AddRequest{Email: "b@x.io", Enable: true})
	_ = f.Add(ctx, tsamx.AddRequest{Email: "c@x.io", Enable: true})
	_ = f.Switch(ctx, "a@x.io")
	// a: both windows, 7d is the max -> exercises ordering + max over windows[].
	f.SetUsage("a@x.io", &tsamx.Usage{FiveHour: &tsamx.Window{Pct: 44}, SevenDay: &tsamx.Window{Pct: 61.2}})
	// b: only five_hour present -> seven_day omitted from windows[].
	f.SetUsage("b@x.io", &tsamx.Usage{FiveHour: &tsamx.Window{Pct: 12}})
	// c: no usage at all -> windows[] empty, contributes 0 to maxPct.

	r := New("ama_test", f, func() time.Time { return time.Unix(1700000000, 0) })
	rep, err := r.BuildUsageReport(ctx, amxv1.UsageReport_TRIGGER_SCHEDULE)
	if err != nil {
		t.Fatal(err)
	}
	byEmail := map[string]*amxv1.AccountUsage{}
	for _, au := range rep.GetAccounts() {
		byEmail[au.GetAccount().GetEmail()] = au
	}

	// a: two windows, ordered five_hour (300) then seven_day (10080), values
	// mirror the positional fields.
	a := byEmail["a@x.io"]
	if got := len(a.GetWindows()); got != 2 {
		t.Fatalf("a windows = %d, want 2", got)
	}
	if a.GetWindows()[0].GetId() != "five_hour" || a.GetWindows()[0].GetWindowMinutes() != 300 || a.GetWindows()[0].GetPct() != 44 {
		t.Fatalf("a windows[0] = %+v", a.GetWindows()[0])
	}
	if a.GetWindows()[1].GetId() != "seven_day" || a.GetWindows()[1].GetWindowMinutes() != 10080 || a.GetWindows()[1].GetPct() != 61.2 {
		t.Fatalf("a windows[1] = %+v", a.GetWindows()[1])
	}
	if a.GetFiveHour().GetPct() != 44 || a.GetSevenDay().GetPct() != 61.2 {
		t.Fatalf("a positional windows not preserved: %+v", a)
	}

	// b: seven_day omitted (nil source), only five_hour recorded.
	b := byEmail["b@x.io"]
	if got := len(b.GetWindows()); got != 1 {
		t.Fatalf("b windows = %d, want 1", got)
	}
	if b.GetWindows()[0].GetId() != "five_hour" {
		t.Fatalf("b windows[0] = %+v", b.GetWindows()[0])
	}
	if b.GetSevenDay() != nil {
		t.Fatalf("b seven_day should be nil, got %+v", b.GetSevenDay())
	}

	// c: no usage -> no windows.
	c := byEmail["c@x.io"]
	if got := len(c.GetWindows()); got != 0 {
		t.Fatalf("c windows = %d, want 0", got)
	}

	// maxUtilizationPct is the max across all present windows (a's 7d = 61.2).
	if got := rep.PoolSummary.MaxUtilizationPct; got != 61.2 {
		t.Fatalf("maxUtilizationPct = %v, want 61.2", got)
	}
}

func TestOutboxDedupeAndFlush(t *testing.T) {
	o := NewOutbox()
	o.Enqueue(&amxv1.AccountEvent{EventId: "e1"})
	o.Enqueue(&amxv1.AccountEvent{EventId: "e1"}) // dup ignored
	o.Enqueue(&amxv1.AccountEvent{EventId: "e2"})
	if o.Depth() != 2 {
		t.Fatalf("depth = %d, want 2", o.Depth())
	}
	var sent []string
	if err := o.Flush(func(ev *amxv1.AccountEvent) error {
		sent = append(sent, ev.GetEventId())
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if len(sent) != 2 || sent[0] != "e1" || sent[1] != "e2" {
		t.Fatalf("flush order = %v", sent)
	}
	if o.Depth() != 0 {
		t.Fatalf("depth after flush = %d", o.Depth())
	}
}

// TestOutboxDedupeWindowBounded: the seen-set that backs event_id dedupe must not
// grow without bound on a long-running agent. After far more than one window of
// distinct events, the map stays capped at outboxDedupeWindow, yet events still
// inside the window are still deduplicated.
func TestOutboxDedupeWindowBounded(t *testing.T) {
	o := NewOutbox()
	const n = outboxDedupeWindow * 3
	for i := 0; i < n; i++ {
		o.Enqueue(&amxv1.AccountEvent{EventId: fmt.Sprintf("e%d", i)})
	}
	if got := len(o.seen); got > outboxDedupeWindow {
		t.Fatalf("seen map size = %d, want <= %d (unbounded growth)", got, outboxDedupeWindow)
	}
	// A recently-seen event_id (last one enqueued) is still deduplicated.
	depth := o.Depth()
	o.Enqueue(&amxv1.AccountEvent{EventId: fmt.Sprintf("e%d", n-1)})
	if o.Depth() != depth {
		t.Fatalf("recent event_id was not deduplicated: depth %d -> %d", depth, o.Depth())
	}
	// An event_id evicted past the window is treated as new (accepted).
	before := o.Depth()
	o.Enqueue(&amxv1.AccountEvent{EventId: "e0"})
	if o.Depth() != before+1 {
		t.Fatalf("evicted event_id not re-accepted: depth %d -> %d", before, o.Depth())
	}
}
