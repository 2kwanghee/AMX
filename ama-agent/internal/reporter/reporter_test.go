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
