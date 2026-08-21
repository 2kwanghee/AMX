package reporter

import (
	"context"
	"encoding/json"
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

// TestBuildUsageReportConsumesNeutralWindows (P3 PR3): the reporter now sources
// every account's windows from the vendor-neutral Usage.Windows list. A
// claude-shaped account (bridge dual-records five_hour/seven_day into Windows)
// still gets the legacy positional fields re-derived; a codex-shaped account
// (Windows carries primary/secondary, no five_hour/seven_day) is reported with
// windows=primary (pct>0) and the positional five_hour/seven_day left nil.
func TestBuildUsageReportConsumesNeutralWindows(t *testing.T) {
	ctx := context.Background()

	// claude-form fake: seed the positional fields; the fake's List projects them
	// into Usage.Windows exactly as the real ExecBridge does.
	claudeFake := tsamx.NewFake()
	_ = claudeFake.Add(ctx, provider.AddRequest{Email: "a@x.io", Enable: true})
	claudeFake.SetUsage("a@x.io", &provider.Usage{
		FiveHour: &provider.Window{Pct: 40}, SevenDay: &provider.Window{Pct: 55},
	})

	// codex-form fake: seed the neutral Windows directly (primary only, no
	// five_hour/seven_day), the shape the codex bridge emits.
	codexFake := tsamx.NewFake()
	_ = codexFake.Add(ctx, provider.AddRequest{Email: "c@x.io", Enable: true})
	codexFake.SetUsage("c@x.io", &provider.Usage{
		Windows: []provider.Window{{Id: "primary", WindowMinutes: 300, Pct: 73}},
	})

	bridges := map[string]provider.Bridge{provider.DefaultProvider: claudeFake, "codex": codexFake}
	r := New("ama_test", bridges, func() time.Time { return time.Unix(1700000000, 0) })
	rep, err := r.BuildUsageReport(ctx, amxv1.UsageReport_TRIGGER_SCHEDULE)
	if err != nil {
		t.Fatal(err)
	}
	byEmail := map[string]*amxv1.AccountUsage{}
	for _, au := range rep.GetAccounts() {
		byEmail[au.GetAccount().GetEmail()] = au
	}

	// claude account: dual-recorded, positional fields present.
	a := byEmail["a@x.io"]
	if got := len(a.GetWindows()); got != 2 {
		t.Fatalf("claude windows = %d, want 2", got)
	}
	if a.GetFiveHour().GetPct() != 40 || a.GetSevenDay().GetPct() != 55 {
		t.Fatalf("claude positional windows not re-derived: %+v", a)
	}

	// codex account: windows=primary with pct>0, positional fields nil.
	c := byEmail["c@x.io"]
	if got := len(c.GetWindows()); got != 1 {
		t.Fatalf("codex windows = %d, want 1", got)
	}
	if c.GetWindows()[0].GetId() != "primary" || c.GetWindows()[0].GetPct() != 73 {
		t.Fatalf("codex windows[0] = %+v, want primary pct 73", c.GetWindows()[0])
	}
	if c.GetFiveHour() != nil || c.GetSevenDay() != nil {
		t.Fatalf("codex account must leave five_hour/seven_day nil, got five_hour=%+v seven_day=%+v", c.GetFiveHour(), c.GetSevenDay())
	}
}

// TestUsageJSONCarriesSpendAndScoped pins the bridge.Usage JSON tags against the
// real tsamx camelCase `list --json` shape: `spend` (used/limit/pct/currency,
// optional resetsAt) and `scoped[]` (per-model weekly windows keyed by `name`)
// must survive json.Unmarshal instead of being dropped as before.
func TestUsageJSONCarriesSpendAndScoped(t *testing.T) {
	const row = `{
	  "fiveHour": {"pct": 10, "resetsAt": "2026-08-15T00:00:00Z"},
	  "sevenDay": {"pct": 20, "resetsAt": "2026-08-20T00:00:00Z"},
	  "spend": {"used": 12.5, "limit": 50, "pct": 25, "currency": "USD",
	            "resetsAt": "2026-09-01T00:00:00Z", "countdown": "16d", "clock": "x"},
	  "scoped": [
	    {"name": "Fable", "pct": 33, "resetsAt": "2026-08-20T00:00:00Z", "aheadOfPace": true},
	    {"name": "Opus", "pct": 44}
	  ]
	}`
	var u provider.Usage
	if err := json.Unmarshal([]byte(row), &u); err != nil {
		t.Fatal(err)
	}
	if u.Spend == nil {
		t.Fatal("spend dropped on unmarshal")
	}
	if u.Spend.Used != 12.5 || u.Spend.Limit != 50 || u.Spend.Pct != 25 || u.Spend.Currency != "USD" {
		t.Fatalf("spend = %+v", *u.Spend)
	}
	if u.Spend.ResetsAt != "2026-09-01T00:00:00Z" {
		t.Fatalf("spend resetsAt = %q", u.Spend.ResetsAt)
	}
	if len(u.Scoped) != 2 {
		t.Fatalf("scoped = %d, want 2", len(u.Scoped))
	}
	if u.Scoped[0].Name != "Fable" || u.Scoped[0].Pct != 33 || u.Scoped[0].ResetsAt != "2026-08-20T00:00:00Z" {
		t.Fatalf("scoped[0] = %+v", u.Scoped[0])
	}
	if u.Scoped[1].Name != "Opus" || u.Scoped[1].Pct != 44 {
		t.Fatalf("scoped[1] = %+v", u.Scoped[1])
	}
}

// TestBuildUsageReportCarriesSpendAndScoped: spend and per-model scoped windows
// flow through to the proto AccountUsage, while the switch/pool math (windows[],
// maxUtilizationPct, eligible, allExhausted) stays exactly what it is without
// them. The scoped 99% below would flip allExhausted if it leaked into windows[].
func TestBuildUsageReportCarriesSpendAndScoped(t *testing.T) {
	f := tsamx.NewFake()
	ctx := context.Background()
	_ = f.Add(ctx, provider.AddRequest{Email: "a@x.io", Enable: true})
	_ = f.Switch(ctx, "a@x.io")
	f.SetUsage("a@x.io", &provider.Usage{
		FiveHour: &provider.Window{Pct: 44},
		SevenDay: &provider.Window{Pct: 61.2},
		Spend:    &provider.Spend{Used: 12.5, Limit: 50, Pct: 25, Currency: "USD", ResetsAt: "2026-09-01T00:00:00Z"},
		Scoped: []provider.ScopedWindow{
			{Name: "Fable", Pct: 99, ResetsAt: "2026-08-20T00:00:00Z"},
			{Name: "Opus", Pct: 10},
		},
	})

	r := New("ama_test", bridgeMap(f), func() time.Time { return time.Unix(1700000000, 0) })
	rep, err := r.BuildUsageReport(ctx, amxv1.UsageReport_TRIGGER_SCHEDULE)
	if err != nil {
		t.Fatal(err)
	}
	var a *amxv1.AccountUsage
	for _, au := range rep.GetAccounts() {
		if au.GetAccount().GetEmail() == "a@x.io" {
			a = au
		}
	}
	if a == nil {
		t.Fatal("account a missing from report")
	}

	// Spend forwarded.
	if a.GetSpend() == nil {
		t.Fatal("spend not carried into proto")
	}
	if a.GetSpend().GetUsed() != 12.5 || a.GetSpend().GetLimit() != 50 || a.GetSpend().GetPct() != 25 || a.GetSpend().GetCurrency() != "USD" {
		t.Fatalf("spend = %+v", a.GetSpend())
	}
	if a.GetSpend().GetResetsAt() == nil {
		t.Fatal("spend resetsAt not parsed")
	}
	// Scoped forwarded in scoped_windows, carrying the model, NOT in windows[].
	if got := len(a.GetScopedWindows()); got != 2 {
		t.Fatalf("scopedWindows = %d, want 2", got)
	}
	if a.GetScopedWindows()[0].GetModel() != "Fable" || a.GetScopedWindows()[0].GetPct() != 99 {
		t.Fatalf("scopedWindows[0] = %+v", a.GetScopedWindows()[0])
	}
	if a.GetScopedWindows()[0].GetResetsAt() == nil {
		t.Fatal("scoped resetsAt not parsed")
	}
	if a.GetScopedWindows()[1].GetModel() != "Opus" || a.GetScopedWindows()[1].GetPct() != 10 {
		t.Fatalf("scopedWindows[1] = %+v", a.GetScopedWindows()[1])
	}

	// windows[] untouched: only the two positional windows, no scoped leak.
	if got := len(a.GetWindows()); got != 2 {
		t.Fatalf("windows = %d, want 2 (scoped must not join windows[])", got)
	}
	for _, w := range a.GetWindows() {
		if w.GetModel() != "" {
			t.Fatalf("positional window carries a model: %+v", w)
		}
	}
	// Switch/pool math is the pre-spend/scoped result: max over windows[] = 61.2,
	// the 99% scoped window did NOT raise it, and the single account stays eligible.
	if got := rep.PoolSummary.MaxUtilizationPct; got != 61.2 {
		t.Fatalf("maxUtilizationPct = %v, want 61.2 (scoped 99 must not count)", got)
	}
	if rep.PoolSummary.Eligible != 1 || rep.PoolSummary.AllExhausted {
		t.Fatalf("pool summary changed by scoped/spend: %+v", rep.PoolSummary)
	}
}

// TestBuildUsageReportQuarantineFromReloginRequired (defect 1): a quarantined
// account surfaces in list --json as usageStatus == "relogin_required" (dead
// refresh-token lineage), NOT the literal "quarantined" the reporter used to
// match. It must count toward PoolSummary.quarantined and carry the QUARANTINED
// allocation status, not be miscounted as an eligible active account.
func TestBuildUsageReportQuarantineFromReloginRequired(t *testing.T) {
	f := tsamx.NewFake()
	ctx := context.Background()
	_ = f.Add(ctx, provider.AddRequest{Email: "good@x.io", Enable: true})
	_ = f.Add(ctx, provider.AddRequest{Email: "dead@x.io", Enable: true})
	_ = f.Switch(ctx, "good@x.io")
	f.SetUsage("good@x.io", &provider.Usage{FiveHour: &provider.Window{Pct: 10}})
	// dead@x.io: quarantined -> relogin_required, no usage measurement.
	f.SetUsageStatus("dead@x.io", "relogin_required")

	r := New("ama_test", bridgeMap(f), func() time.Time { return time.Unix(1700000000, 0) })
	rep, err := r.BuildUsageReport(ctx, amxv1.UsageReport_TRIGGER_SCHEDULE)
	if err != nil {
		t.Fatal(err)
	}
	if rep.PoolSummary.Quarantined != 1 {
		t.Fatalf("quarantined = %d, want 1 (relogin_required must count)", rep.PoolSummary.Quarantined)
	}
	// Only the measured good account is eligible; the quarantined one is not.
	if rep.PoolSummary.Eligible != 1 {
		t.Fatalf("eligible = %d, want 1 (quarantined must not be eligible)", rep.PoolSummary.Eligible)
	}
	if rep.PoolSummary.Active != 1 {
		t.Fatalf("active = %d, want 1 (quarantined is not an active candidate)", rep.PoolSummary.Active)
	}
	var dead *amxv1.AccountUsage
	for _, au := range rep.GetAccounts() {
		if au.GetAccount().GetEmail() == "dead@x.io" {
			dead = au
		}
	}
	if dead.GetAllocationStatus() != amxv1.AllocationStatus_ALLOCATION_STATUS_QUARANTINED {
		t.Fatalf("dead allocation status = %v, want QUARANTINED", dead.GetAllocationStatus())
	}
}

// TestBuildUsageReportUnmeasuredNotEligible (defect 2): an account with null usage
// (unmeasured — token_expired here) has empty windows and thus pct 0, which used
// to read as eligible and, on its own, keep all_exhausted false. It must be
// excluded from eligible and must neither drive nor relieve all_exhausted; the
// signal is decided over MEASURED accounts only.
func TestBuildUsageReportUnmeasuredNotEligible(t *testing.T) {
	f := tsamx.NewFake()
	ctx := context.Background()
	_ = f.Add(ctx, provider.AddRequest{Email: "hot@x.io", Enable: true})
	_ = f.Add(ctx, provider.AddRequest{Email: "unknown@x.io", Enable: true})
	_ = f.Switch(ctx, "hot@x.io")
	// hot: measured and exhausted (>= threshold).
	f.SetUsage("hot@x.io", &provider.Usage{FiveHour: &provider.Window{Pct: 99}})
	// unknown: token expired, no measurement.
	f.SetUsageStatus("unknown@x.io", "token_expired")

	r := New("ama_test", bridgeMap(f), func() time.Time { return time.Unix(1700000000, 0) })
	rep, err := r.BuildUsageReport(ctx, amxv1.UsageReport_TRIGGER_SCHEDULE)
	if err != nil {
		t.Fatal(err)
	}
	if rep.PoolSummary.Eligible != 0 {
		t.Fatalf("eligible = %d, want 0 (unmeasured must not be eligible)", rep.PoolSummary.Eligible)
	}
	// One measured account, exhausted; the unmeasured one must not relieve it.
	if !rep.PoolSummary.AllExhausted {
		t.Fatal("allExhausted should be true: the sole measured account is exhausted")
	}

	// With NO measured account, all_exhausted must be false (nothing to conclude).
	f.SetUsageStatus("hot@x.io", "unavailable")
	rep2, err := r.BuildUsageReport(ctx, amxv1.UsageReport_TRIGGER_SCHEDULE)
	if err != nil {
		t.Fatal(err)
	}
	if rep2.PoolSummary.AllExhausted {
		t.Fatal("allExhausted should be false when no account is measured")
	}
	if rep2.PoolSummary.Eligible != 0 {
		t.Fatalf("eligible = %d, want 0 (all unmeasured)", rep2.PoolSummary.Eligible)
	}
}

// TestBuildUsageReportUsageFetchedAt (defect 3): the tsamx usageFetchedAt freshness
// stamp is carried into proto AccountUsage.usage_fetched_at; a row without it
// leaves the field nil.
func TestBuildUsageReportUsageFetchedAt(t *testing.T) {
	f := tsamx.NewFake()
	ctx := context.Background()
	_ = f.Add(ctx, provider.AddRequest{Email: "a@x.io", Enable: true})
	_ = f.Add(ctx, provider.AddRequest{Email: "b@x.io", Enable: true})
	f.SetUsage("a@x.io", &provider.Usage{FiveHour: &provider.Window{Pct: 10}})
	f.SetUsage("b@x.io", &provider.Usage{FiveHour: &provider.Window{Pct: 20}})
	f.SetUsageFetchedAt("a@x.io", "2026-08-21T12:34:56Z")
	// b@x.io left without a stamp.

	r := New("ama_test", bridgeMap(f), func() time.Time { return time.Unix(1700000000, 0) })
	rep, err := r.BuildUsageReport(ctx, amxv1.UsageReport_TRIGGER_SCHEDULE)
	if err != nil {
		t.Fatal(err)
	}
	byEmail := map[string]*amxv1.AccountUsage{}
	for _, au := range rep.GetAccounts() {
		byEmail[au.GetAccount().GetEmail()] = au
	}
	got := byEmail["a@x.io"].GetUsageFetchedAt()
	if got == nil {
		t.Fatal("a usage_fetched_at not populated from usageFetchedAt")
	}
	if want := time.Date(2026, 8, 21, 12, 34, 56, 0, time.UTC); !got.AsTime().Equal(want) {
		t.Fatalf("a usage_fetched_at = %v, want %v", got.AsTime(), want)
	}
	if byEmail["b@x.io"].GetUsageFetchedAt() != nil {
		t.Fatalf("b usage_fetched_at should be nil without a stamp, got %v", byEmail["b@x.io"].GetUsageFetchedAt())
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
