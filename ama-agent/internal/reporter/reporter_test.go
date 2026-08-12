package reporter

import (
	"context"
	"fmt"
	"testing"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
	"github.com/2kwanghee/AMX/ama-agent/internal/tsamx"
	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
)

// bridgeMap wraps a single fake as the claude-provider registry the reporter now
// takes, so the tests read as before.
func bridgeMap(b provider.Bridge) map[string]provider.Bridge {
	return map[string]provider.Bridge{provider.DefaultProvider: b}
}

func TestBuildUsageReport(t *testing.T) {
	f := tsamx.NewFake()
	ctx := context.Background()
	_ = f.Add(ctx, provider.AddRequest{Email: "a@x.io", Enable: true})
	_ = f.Add(ctx, provider.AddRequest{Email: "b@x.io", Enable: true})
	_ = f.Switch(ctx, "a@x.io")
	f.SetUsage("a@x.io", &provider.Usage{FiveHour: &provider.Window{Pct: 61.2}, SevenDay: &provider.Window{Pct: 44}})
	f.SetUsage("b@x.io", &provider.Usage{FiveHour: &provider.Window{Pct: 12}})

	r := New("ama_test", bridgeMap(f), func() time.Time { return time.Unix(1700000000, 0) })
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
	_ = f.Add(ctx, provider.AddRequest{Email: "known@x.io", Enable: true})
	_ = f.Add(ctx, provider.AddRequest{Email: "stranger@x.io", Enable: true})

	r := New("ama_test", bridgeMap(f), func() time.Time { return time.Unix(1700000000, 0) })
	// Resolver models the manifest: only "known@x.io" is an AMS-assigned account.
	r.SetIDResolver(func(providerKey, email string) (string, string, bool) {
		if providerKey == provider.DefaultProvider && email == "known@x.io" {
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
	_ = f.Add(ctx, provider.AddRequest{Email: "a@x.io", Enable: true})

	r := New("ama_test", bridgeMap(f), func() time.Time { return time.Unix(1700000000, 0) })
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
	_ = f.Add(ctx, provider.AddRequest{Email: "a@x.io", Enable: true})
	_ = f.Add(ctx, provider.AddRequest{Email: "b@x.io", Enable: true})
	_ = f.Add(ctx, provider.AddRequest{Email: "c@x.io", Enable: true})
	_ = f.Switch(ctx, "a@x.io")
	// a: both windows, 7d is the max -> exercises ordering + max over windows[].
	f.SetUsage("a@x.io", &provider.Usage{FiveHour: &provider.Window{Pct: 44}, SevenDay: &provider.Window{Pct: 61.2}})
	// b: only five_hour present -> seven_day omitted from windows[].
	f.SetUsage("b@x.io", &provider.Usage{FiveHour: &provider.Window{Pct: 12}})
	// c: no usage at all -> windows[] empty, contributes 0 to maxPct.

	r := New("ama_test", bridgeMap(f), func() time.Time { return time.Unix(1700000000, 0) })
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

// TestBuildUsageReportStampsProvider (P3 Claude-invariance golden): with only the
// claude provider registered, the report is byte-for-byte what it was before the
// shim EXCEPT that every account (and the active-account ref) now carries
// provider="claude". This pins the "Claude behavior unchanged" contract: the same
// pool projects to the same totals/windows, plus the single new stamp.
func TestBuildUsageReportStampsProvider(t *testing.T) {
	f := tsamx.NewFake()
	ctx := context.Background()
	_ = f.Add(ctx, provider.AddRequest{Email: "a@x.io", Enable: true})
	_ = f.Add(ctx, provider.AddRequest{Email: "b@x.io", Enable: true})
	_ = f.Switch(ctx, "a@x.io")
	f.SetUsage("a@x.io", &provider.Usage{FiveHour: &provider.Window{Pct: 61.2}, SevenDay: &provider.Window{Pct: 44}})
	f.SetUsage("b@x.io", &provider.Usage{FiveHour: &provider.Window{Pct: 12}})

	r := New("ama_test", bridgeMap(f), func() time.Time { return time.Unix(1700000000, 0) })
	rep, err := r.BuildUsageReport(ctx, amxv1.UsageReport_TRIGGER_SCHEDULE)
	if err != nil {
		t.Fatal(err)
	}
	// Unchanged aggregate projection (identical to TestBuildUsageReport).
	if rep.PoolSummary.Total != 2 || rep.PoolSummary.Eligible != 2 || rep.PoolSummary.AllExhausted {
		t.Fatalf("pool summary changed: %+v", rep.PoolSummary)
	}
	if got := rep.PoolSummary.MaxUtilizationPct; got != 61.2 {
		t.Fatalf("maxUtilizationPct = %v, want 61.2", got)
	}
	// The one new field: every account carries the claude provider stamp.
	for _, au := range rep.GetAccounts() {
		if au.GetAccount().GetProvider() != provider.DefaultProvider {
			t.Fatalf("account %s provider = %q, want %q", au.GetAccount().GetEmail(), au.GetAccount().GetProvider(), provider.DefaultProvider)
		}
	}
	if rep.GetActiveAccount().GetProvider() != provider.DefaultProvider {
		t.Fatalf("active account provider = %q, want %q", rep.GetActiveAccount().GetProvider(), provider.DefaultProvider)
	}
}

// TestBuildUsageReportSummaryScopedToAutoSwitchProvider (PR1b review B item 1):
// PoolSummary and ActiveAccount reflect ONLY the auto-switch provider (claude),
// while accounts[] carries every provider. A non-rotating codex account with zero
// usage must NOT count toward eligible/allExhausted (which would jam the
// scheduler's AllExhausted alert) and must NOT overwrite ActiveAccount.
func TestBuildUsageReportSummaryScopedToAutoSwitchProvider(t *testing.T) {
	ctx := context.Background()
	// claude: a single active account, exhausted (>= threshold) -> AllExhausted.
	claudeFake := tsamx.NewFake()
	_ = claudeFake.Add(ctx, provider.AddRequest{Email: "a@x.io", Enable: true})
	claudeFake.SetUsage("a@x.io", &provider.Usage{FiveHour: &provider.Window{Pct: 99}})
	// codex: an account with zero usage (would read as eligible if summed in) and
	// active in its own pool (would clobber ActiveAccount if not scoped out).
	codexFake := tsamx.NewFake()
	_ = codexFake.Add(ctx, provider.AddRequest{Email: "c@x.io", Enable: true})

	bridges := map[string]provider.Bridge{provider.DefaultProvider: claudeFake, "codex": codexFake}
	r := New("ama_test", bridges, func() time.Time { return time.Unix(1700000000, 0) })
	rep, err := r.BuildUsageReport(ctx, amxv1.UsageReport_TRIGGER_SCHEDULE)
	if err != nil {
		t.Fatal(err)
	}

	// Summary is claude-only: 1 account, exhausted.
	if rep.PoolSummary.Total != 1 {
		t.Fatalf("summary total = %d, want 1 (claude only)", rep.PoolSummary.Total)
	}
	if rep.PoolSummary.Eligible != 0 {
		t.Fatalf("summary eligible = %d, want 0 (codex must not count)", rep.PoolSummary.Eligible)
	}
	if !rep.PoolSummary.AllExhausted {
		t.Fatal("allExhausted should be true: the only claude account is exhausted; codex must not relieve it")
	}
	if rep.GetActiveAccount().GetEmail() != "a@x.io" || rep.GetActiveAccount().GetProvider() != provider.DefaultProvider {
		t.Fatalf("active account = %+v, want claude a@x.io", rep.GetActiveAccount())
	}
	// accounts[] is the full aggregate: both providers, each stamped.
	byEmail := map[string]string{}
	for _, au := range rep.GetAccounts() {
		byEmail[au.GetAccount().GetEmail()] = au.GetAccount().GetProvider()
	}
	if len(byEmail) != 2 || byEmail["a@x.io"] != provider.DefaultProvider || byEmail["c@x.io"] != "codex" {
		t.Fatalf("accounts[] not full aggregate with provider stamps: %v", byEmail)
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
