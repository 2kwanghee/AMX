package autoswitch

import (
	"errors"
	"testing"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
	"github.com/2kwanghee/AMX/ama-agent/internal/seat/profile"
	seatusage "github.com/2kwanghee/AMX/ama-agent/internal/seat/usage"
)

// usage builds a provider.Usage the way tsamx/tests/test_autoswitch.py's
// `_usage(pct)` helper does: five_hour.pct = pct, seven_day.pct = 0 (so the
// binding window is always five_hour, headroom = 100-pct), optionally with
// a five_hour resetsAt (RFC3339) for recovery-axis tests.
func usage(pct float64, resetsAt string) *provider.Usage {
	return &provider.Usage{
		FiveHour: &provider.Window{Pct: pct, ResetsAt: resetsAt},
		SevenDay: &provider.Window{Pct: 0},
	}
}

// usageWeekly builds a provider.Usage with an explicit seven-day window
// (for consume-first tests, which rank on that window's reset).
func usageWeekly(fiveHourPct, sevenDayPct float64, sevenDayResetsAt string) *provider.Usage {
	return &provider.Usage{
		FiveHour: &provider.Window{Pct: fiveHourPct},
		SevenDay: &provider.Window{Pct: sevenDayPct, ResetsAt: sevenDayResetsAt},
	}
}

func acct(number int, email string, pct float64, resetsAt string) provider.AccountRow {
	return provider.AccountRow{Number: number, Email: email, Usage: usage(pct, resetsAt), UsageStatus: "ok"}
}

func key(email string) string { return profile.AccountKey(email) }

var t0 = time.Date(2026, 8, 23, 0, 0, 0, 0, time.UTC)

func mustSwitch(t *testing.T, d Decision, err error, wantTo int) {
	t.Helper()
	if err != nil {
		t.Fatalf("Decide error: %v", err)
	}
	if d.Outcome != OutcomeSwitched {
		t.Fatalf("outcome = %v, want Switched (reason=%s detail=%s)", d.Outcome, d.Reason, d.Detail)
	}
	if d.To == nil || d.To.Number != wantTo {
		t.Fatalf("to = %+v, want account %d", d.To, wantTo)
	}
	if d.To.AccountKey == "" {
		t.Fatalf("to.AccountKey is empty, want profile.AccountKey(email)")
	}
}

func mustBlocked(t *testing.T, d Decision, err error, wantReason string) {
	t.Helper()
	if err != nil {
		t.Fatalf("Decide error: %v", err)
	}
	if d.Outcome != OutcomeBlocked {
		t.Fatalf("outcome = %v, want Blocked (reason=%s)", d.Outcome, d.Reason)
	}
	if d.Reason != wantReason {
		t.Fatalf("reason = %q, want %q", d.Reason, wantReason)
	}
}

func mustNoAction(t *testing.T, d Decision, err error, wantReason string) {
	t.Helper()
	if err != nil {
		t.Fatalf("Decide error: %v", err)
	}
	if d.Outcome != OutcomeNoAction {
		t.Fatalf("outcome = %v, want NoAction (reason=%s)", d.Outcome, d.Reason)
	}
	if d.Reason != wantReason {
		t.Fatalf("reason = %q, want %q", d.Reason, wantReason)
	}
}

// --- 임계값(threshold) 경계 ---------------------------------------------

func TestDecide_ThresholdBoundary(t *testing.T) {
	pol := Policy{ThresholdPct: 90, HysteresisPct: 10, CooldownSeconds: 300, UnhealthyTicks: 3}
	below := Input{
		Accounts:     []provider.AccountRow{acct(1, "a@x", 89.999, ""), acct(2, "b@x", 0, "")},
		ActiveNumber: 1, Strategy: StrategyBest, Policy: pol, Now: t0,
	}
	d, err := Decide(below)
	mustNoAction(t, d, err, "below-threshold")

	at := below
	at.Accounts = []provider.AccountRow{acct(1, "a@x", 90.0, ""), acct(2, "b@x", 0, "")}
	d, err = Decide(at)
	mustSwitch(t, d, err, 2)
}

// --- 안티 플랩 1: hysteresis_pct 경계 (< vs ==) --------------------------

func TestDecide_HysteresisBoundary(t *testing.T) {
	pol := Policy{ThresholdPct: 90, HysteresisPct: 10, CooldownSeconds: 300, UnhealthyTicks: 3}
	qualifies := Input{
		Accounts:     []provider.AccountRow{acct(1, "a@x", 95, ""), acct(2, "b@x", 85, "")}, // headroom 15
		ActiveNumber: 1, Strategy: StrategyBest, Policy: pol, Now: t0,
	}
	d, err := Decide(qualifies)
	mustSwitch(t, d, err, 2)

	rejects := qualifies
	rejects.Accounts = []provider.AccountRow{acct(1, "a@x", 95, ""), acct(2, "b@x", 85.001, "")}
	d, err = Decide(rejects)
	mustBlocked(t, d, err, "no-qualifying-candidate")
}

// --- 안티 플랩 2: cooldown_seconds 경계 (< vs ==) -------------------------

func TestDecide_CooldownBoundary(t *testing.T) {
	pol := Policy{ThresholdPct: 90, HysteresisPct: 10, CooldownSeconds: 120, UnhealthyTicks: 3}
	accounts := []provider.AccountRow{acct(1, "a@x", 95, ""), acct(2, "b@x", 80, "")}
	last := t0.Add(-120 * time.Second)
	in := Input{Accounts: accounts, ActiveNumber: 1, Strategy: StrategyBest, Policy: pol, Now: t0, LastSwitchAt: &last}
	d, err := Decide(in)
	mustSwitch(t, d, err, 2)

	last2 := t0.Add(-119 * time.Second)
	in2 := in
	in2.LastSwitchAt = &last2
	d, err = Decide(in2)
	mustNoAction(t, d, err, "cooldown")

	atLimit := []provider.AccountRow{acct(1, "a@x", 100, ""), acct(2, "b@x", 80, "")}
	in3 := Input{Accounts: atLimit, ActiveNumber: 1, Strategy: StrategyBest, Policy: pol, Now: t0, LastSwitchAt: &last2}
	d, err = Decide(in3)
	mustSwitch(t, d, err, 2)
	if d.Trigger != "at-limit" {
		t.Fatalf("trigger = %q, want at-limit", d.Trigger)
	}
}

// --- 안티 플랩 3: 전원 임계 초과 탈출(_recovery_is_useful) 및 전원 소진 --

func TestDecide_RecoveryEscape(t *testing.T) {
	pol := Policy{ThresholdPct: 90, HysteresisPct: 10, CooldownSeconds: 0, UnhealthyTicks: 3}
	activeResets := t0.Add(10 * time.Hour).Format(time.RFC3339)
	candResets := t0.Add(2 * time.Hour).Format(time.RFC3339)
	escapes := Input{
		Accounts: []provider.AccountRow{
			acct(1, "a@x", 95, activeResets),
			acct(2, "b@x", 95, candResets),
		},
		ActiveNumber: 1, Strategy: StrategyBest, Policy: pol, Now: t0,
	}
	d, err := Decide(escapes)
	mustSwitch(t, d, err, 2)
	if d.Trigger != "proactive" {
		t.Fatalf("trigger = %q, want proactive", d.Trigger)
	}

	activeAt := t0.Add(10000 * time.Second).Format(time.RFC3339)
	candAtBoundary := t0.Add(9700 * time.Second).Format(time.RFC3339) // == active-300, rejected
	boundary := Input{
		Accounts: []provider.AccountRow{
			acct(1, "a@x", 95, activeAt),
			acct(2, "b@x", 95, candAtBoundary),
		},
		ActiveNumber: 1, Strategy: StrategyBest, Policy: pol, Now: t0,
	}
	d, err = Decide(boundary)
	mustBlocked(t, d, err, "no-qualifying-candidate")

	candAtPass := t0.Add(9699 * time.Second).Format(time.RFC3339)
	pass := boundary
	pass.Accounts = []provider.AccountRow{
		acct(1, "a@x", 95, activeAt),
		acct(2, "b@x", 95, candAtPass),
	}
	d, err = Decide(pass)
	mustSwitch(t, d, err, 2)

	exhausted := Input{
		Accounts: []provider.AccountRow{
			acct(1, "a@x", 100, ""),
			acct(2, "b@x", 100, ""),
		},
		ActiveNumber: 1, Strategy: StrategyBest, Policy: pol, Now: t0,
	}
	d, err = Decide(exhausted)
	mustBlocked(t, d, err, "all-exhausted")
	if d.ResultCode(nil) != CodeBlocked {
		t.Fatalf("ResultCode = %d, want %d", d.ResultCode(nil), CodeBlocked)
	}
}

// --- 격리 판정 (ShouldQuarantine): relogin_required만 격리 ---------------

func TestShouldQuarantine_ReloginVsExpired(t *testing.T) {
	if !ShouldQuarantine(seatusage.StatusReloginRequired) {
		t.Fatalf("relogin_required must quarantine")
	}
	if ShouldQuarantine(seatusage.StatusTokenExpired) {
		t.Fatalf("token_expired must NOT quarantine (P4 review defect this port must not repeat)")
	}
	if ShouldQuarantine("ok") || ShouldQuarantine("") {
		t.Fatalf("healthy/unknown status must not quarantine")
	}
}

func TestDecide_NewQuarantineSweep(t *testing.T) {
	pol := Policy{ThresholdPct: 90, HysteresisPct: 10, CooldownSeconds: 0, UnhealthyTicks: 3}
	dead := acct(2, "dead@x", 0, "")
	dead.UsageStatus = seatusage.StatusReloginRequired
	dead.Usage = nil // contract C4: usage==null alongside relogin_required
	expired := acct(3, "expired@x", 0, "")
	expired.UsageStatus = seatusage.StatusTokenExpired
	expired.Usage = nil                     // unmeasured, but NOT quarantined
	healthy := acct(4, "healthy@x", 80, "") // headroom 20, clears hysteresis of 10 easily

	in := Input{
		Accounts:     []provider.AccountRow{acct(1, "a@x", 95, ""), dead, expired, healthy},
		ActiveNumber: 1, Strategy: StrategyBest, Policy: pol, Now: t0,
	}
	d, err := Decide(in)
	mustSwitch(t, d, err, 4)
	if len(d.NewQuarantine) != 1 || d.NewQuarantine[0].AccountKey != key("dead@x") {
		t.Fatalf("NewQuarantine = %+v, want exactly dead@x's key", d.NewQuarantine)
	}
	if len(d.Released) != 0 {
		t.Fatalf("Released = %+v, want none", d.Released)
	}
}

// --- C1 회귀: 격리 해제는 지문/계정교체로만, 상태 회복만으로는 해제 금지 --

func TestDecide_QuarantineRelease_StatusAloneDoesNotRelease(t *testing.T) {
	// The account is quarantined; its usageStatus has since gone back to
	// "ok" (e.g. it simply stopped being fetched, or P4 reported something
	// other than relogin_required this tick) but NEITHER its email nor its
	// fingerprint changed. Review C1: this must NOT release the quarantine
	// — a dead refresh-token lineage does not heal itself just because the
	// reported status changed.
	pol := Policy{ThresholdPct: 90, HysteresisPct: 10, CooldownSeconds: 0, UnhealthyTicks: 3}
	recoveredStatus := acct(2, "dead@x", 30, "") // headroom 70 if it were a candidate
	recoveredStatus.UsageStatus = "ok"
	healthy := acct(3, "healthy@x", 80, "") // headroom 20

	in := Input{
		Accounts:     []provider.AccountRow{acct(1, "a@x", 95, ""), recoveredStatus, healthy},
		ActiveNumber: 1, Strategy: StrategyBest, Policy: pol, Now: t0,
		Quarantine: map[string]QuarantineEntry{
			key("dead@x"): {Email: "dead@x", Reason: "relogin_required", RefreshTokenFingerprint: "fp-1"},
		},
		// No Fingerprints supplied -> "not computed this tick", so the
		// fingerprint check alone can never release (see ShouldRelease doc).
	}
	d, err := Decide(in)
	// account 2 (headroom 70) must NOT be selected — it stays quarantined,
	// so the only candidate is account 3 (headroom 20).
	mustSwitch(t, d, err, 3)
	if len(d.Released) != 0 {
		t.Fatalf("Released = %+v, want none (status recovery alone must not release, review C1)", d.Released)
	}
}

func TestDecide_QuarantineRelease_FingerprintChanged(t *testing.T) {
	pol := Policy{ThresholdPct: 90, HysteresisPct: 10, CooldownSeconds: 0, UnhealthyTicks: 3}
	replaced := acct(2, "dead@x", 30, "") // headroom 70, best target once released
	in := Input{
		Accounts:     []provider.AccountRow{acct(1, "a@x", 95, ""), replaced, acct(3, "healthy@x", 80, "")},
		ActiveNumber: 1, Strategy: StrategyBest, Policy: pol, Now: t0,
		Quarantine: map[string]QuarantineEntry{
			key("dead@x"): {Email: "dead@x", Reason: "relogin_required", RefreshTokenFingerprint: "old-fp"},
		},
		Fingerprints: map[string]string{key("dead@x"): "new-fp"},
	}
	d, err := Decide(in)
	mustSwitch(t, d, err, 2) // released and picked: headroom 70 beats account 3's 20
	if len(d.Released) != 1 || d.Released[0].Reason != "credentials-replaced" {
		t.Fatalf("Released = %+v, want exactly one credentials-replaced entry", d.Released)
	}
}

func TestDecide_QuarantineRelease_AccountRemoved(t *testing.T) {
	// The quarantined slot no longer appears in Accounts at all (removed
	// from the pool) -> released as "account-replaced" (review 발견물: a
	// removed account's quarantine entry must not persist forever).
	pol := Policy{ThresholdPct: 90, HysteresisPct: 10, CooldownSeconds: 0, UnhealthyTicks: 3}
	in := Input{
		Accounts:     []provider.AccountRow{acct(1, "a@x", 95, ""), acct(3, "healthy@x", 80, "")},
		ActiveNumber: 1, Strategy: StrategyBest, Policy: pol, Now: t0,
		Quarantine: map[string]QuarantineEntry{
			key("gone@x"): {Email: "gone@x", Reason: "relogin_required"},
		},
	}
	d, err := Decide(in)
	mustSwitch(t, d, err, 3)
	if len(d.Released) != 1 || d.Released[0].Reason != "account-replaced" {
		t.Fatalf("Released = %+v, want exactly one account-replaced entry", d.Released)
	}
}

// --- C2 회귀: unhealthy_ticks 페일오버로 영구 동결 탈출 -------------------

func TestDecide_UnhealthyTicksFailover(t *testing.T) {
	pol := Policy{ThresholdPct: 90, HysteresisPct: 10, CooldownSeconds: 0, UnhealthyTicks: 3}
	unreadableActive := provider.AccountRow{Number: 1, Email: "a@x", UsageStatus: "unavailable", Usage: nil}
	healthy := acct(2, "b@x", 50, "") // headroom 50, clearly healthy

	accounts := []provider.AccountRow{unreadableActive, healthy}

	// tick 1: 0 -> 1, still below UnhealthyTicks(3) -> NoAction.
	d, err := Decide(Input{Accounts: accounts, ActiveNumber: 1, Strategy: StrategyBest, Policy: pol, Now: t0, ConsecutiveUnhealthyTicks: 0})
	mustNoAction(t, d, err, "active-usage-unknown")
	if d.UnhealthyTicks != 1 || d.Detail != "1/3 before failover" {
		t.Fatalf("tick1 UnhealthyTicks/Detail = %d/%q, want 1/\"1/3 before failover\"", d.UnhealthyTicks, d.Detail)
	}

	// tick 2: 1 -> 2, still below threshold -> NoAction.
	d, err = Decide(Input{Accounts: accounts, ActiveNumber: 1, Strategy: StrategyBest, Policy: pol, Now: t0, ConsecutiveUnhealthyTicks: 1})
	mustNoAction(t, d, err, "active-usage-unknown")
	if d.UnhealthyTicks != 2 {
		t.Fatalf("tick2 UnhealthyTicks = %d, want 2", d.UnhealthyTicks)
	}

	// tick 3: 2 -> 3, reaches UnhealthyTicks(3) -> failover, escapes to the
	// healthy candidate. This is the exact scenario review C2 flagged as a
	// permanent freeze in the first version (exit code 2 forever, never
	// escalating) — must now switch.
	d, err = Decide(Input{Accounts: accounts, ActiveNumber: 1, Strategy: StrategyBest, Policy: pol, Now: t0, ConsecutiveUnhealthyTicks: 2})
	mustSwitch(t, d, err, 2)
	if d.Trigger != "failover" {
		t.Fatalf("trigger = %q, want failover", d.Trigger)
	}
	if d.UnhealthyTicks != 3 {
		t.Fatalf("tick3 UnhealthyTicks = %d, want 3", d.UnhealthyTicks)
	}

	// Once the active account's usage becomes readable again, the counter
	// resets to 0 (autoswitch.py:941).
	readableAgain := acct(1, "a@x", 50, "")
	d, err = Decide(Input{Accounts: []provider.AccountRow{readableAgain, healthy}, ActiveNumber: 1, Strategy: StrategyBest, Policy: pol, Now: t0, ConsecutiveUnhealthyTicks: 2})
	mustNoAction(t, d, err, "below-threshold")
	if d.UnhealthyTicks != 0 {
		t.Fatalf("UnhealthyTicks after recovery = %d, want 0", d.UnhealthyTicks)
	}
}

// --- C3 회귀: next-available은 tick 전략이 아니라 명시적으로 거부 --------

func TestDecide_NextAvailableRejected(t *testing.T) {
	pol := Policy{ThresholdPct: 90, HysteresisPct: 10, CooldownSeconds: 0, UnhealthyTicks: 3}
	in := Input{
		Accounts:     []provider.AccountRow{acct(1, "a@x", 95, ""), acct(2, "b@x", 50, "")},
		ActiveNumber: 1, Strategy: StrategyNextAvailable, Policy: pol, Now: t0,
	}
	d, err := Decide(in)
	if !errors.Is(err, ErrNextAvailableIsManualOnly) {
		t.Fatalf("err = %v, want ErrNextAvailableIsManualOnly", err)
	}
	if d.Outcome != OutcomeNoAction || d.To != nil || d.From != nil || len(d.Events) != 0 {
		t.Fatalf("d = %+v, want zero Decision on rejection", d)
	}
	if d.ResultCode(err) != CodeError {
		t.Fatalf("ResultCode = %d, want CodeError", d.ResultCode(err))
	}
}

// --- C3: consume-first 전략 이식 -----------------------------------------

func TestDecide_ConsumeFirstStrategy(t *testing.T) {
	pol := Policy{ThresholdPct: 90, HysteresisPct: 10, CooldownSeconds: 0, UnhealthyTicks: 3}

	// Below threshold (util 50 < 90), but consume-first still evaluates:
	// the candidate's weekly window resets sooner than the active's ->
	// switches, trigger "consume-first".
	activeReset := t0.Add(100000 * time.Second).Format(time.RFC3339)
	soonerReset := t0.Add(50000 * time.Second).Format(time.RFC3339)
	sooner := Input{
		Accounts: []provider.AccountRow{
			{Number: 1, Email: "a@x", UsageStatus: "ok", Usage: usageWeekly(50, 10, activeReset)},
			{Number: 2, Email: "b@x", UsageStatus: "ok", Usage: usageWeekly(50, 10, soonerReset)},
		},
		ActiveNumber: 1, Strategy: StrategyConsumeFirst, Policy: pol, Now: t0,
	}
	d, err := Decide(sooner)
	mustSwitch(t, d, err, 2)
	if d.Trigger != "consume-first" {
		t.Fatalf("trigger = %q, want consume-first", d.Trigger)
	}

	// Candidate resets LATER than active -> stays (NoAction, not Blocked —
	// this is a healthy hold, not an exhaustion state, autoswitch.py:
	// 1224-1230).
	laterReset := t0.Add(200000 * time.Second).Format(time.RFC3339)
	later := sooner
	later.Accounts = []provider.AccountRow{
		{Number: 1, Email: "a@x", UsageStatus: "ok", Usage: usageWeekly(50, 10, activeReset)},
		{Number: 2, Email: "b@x", UsageStatus: "ok", Usage: usageWeekly(50, 10, laterReset)},
	}
	d, err = Decide(later)
	mustNoAction(t, d, err, "already-consuming-soonest")

	// Active's own weekly reset is unknown -> "reset-unknown", NoAction.
	unknown := sooner
	unknown.Accounts = []provider.AccountRow{
		{Number: 1, Email: "a@x", UsageStatus: "ok", Usage: usageWeekly(50, 10, "")},
		{Number: 2, Email: "b@x", UsageStatus: "ok", Usage: usageWeekly(50, 10, soonerReset)},
	}
	d, err = Decide(unknown)
	mustNoAction(t, d, err, "reset-unknown")
}

// --- 선택 전략 결정성(동점 처리) ------------------------------------------

func TestDecide_StrategyDeterminism(t *testing.T) {
	pol := Policy{ThresholdPct: 90, HysteresisPct: 10, CooldownSeconds: 0, UnhealthyTicks: 3}
	tie := Input{
		Accounts: []provider.AccountRow{
			acct(1, "a@x", 95, ""),
			acct(3, "c@x", 60, ""), // headroom 40
			acct(2, "b@x", 60, ""), // headroom 40, same as 3 but lower number
		},
		ActiveNumber: 1, Strategy: StrategyBest, Policy: pol, Now: t0,
	}
	for i := 0; i < 5; i++ {
		d, err := Decide(tie)
		mustSwitch(t, d, err, 2)
	}

	// consume-first tie: two candidates with the identical weekly reset ->
	// deterministic tie-break by account Number.
	sameReset := t0.Add(50000 * time.Second).Format(time.RFC3339)
	activeReset := t0.Add(100000 * time.Second).Format(time.RFC3339)
	cfTie := Input{
		Accounts: []provider.AccountRow{
			{Number: 1, Email: "a@x", UsageStatus: "ok", Usage: usageWeekly(50, 10, activeReset)},
			{Number: 3, Email: "c@x", UsageStatus: "ok", Usage: usageWeekly(50, 10, sameReset)},
			{Number: 2, Email: "b@x", UsageStatus: "ok", Usage: usageWeekly(50, 10, sameReset)},
		},
		ActiveNumber: 1, Strategy: StrategyConsumeFirst, Policy: pol, Now: t0,
	}
	for i := 0; i < 5; i++ {
		d, err := Decide(cfTie)
		mustSwitch(t, d, err, 2)
	}
}

// --- 결과 코드 매핑 4종 ---------------------------------------------------

func TestDecision_ResultCode(t *testing.T) {
	cases := []struct {
		d    Decision
		err  error
		want ResultCode
	}{
		{Decision{Outcome: OutcomeSwitched}, nil, CodeSwitched},
		{Decision{Outcome: OutcomeNoAction}, nil, CodeNoAction},
		{Decision{Outcome: OutcomeBlocked}, nil, CodeBlocked},
		{Decision{Outcome: OutcomeSwitched}, errTest, CodeError}, // error always wins, contract C5
	}
	for _, c := range cases {
		if got := c.d.ResultCode(c.err); got != c.want {
			t.Fatalf("ResultCode(%v) with outcome %v = %d, want %d", c.err, c.d.Outcome, got, c.want)
		}
	}
}

var errTest = &testError{}

type testError struct{}

func (*testError) Error() string { return "boom" }

// --- 원본 tsamx 테스트와 동일 입력 -> 동일 출력 (골든 케이스) -------------

// TestDecide_GoldenHysteresisMarginBlocks replicates
// tsamx/tests/test_autoswitch.py::test_hysteresis_margin_blocks_marginal_candidates
// (default threshold=90/hysteresis=10 from tsamx/src/tsamx/settings.py:46-49):
// active 95% (headroom 5), candidates 86%/88% (headroom 14/12). Neither
// clears the 10-point margin (14-5=9 < 10; 12-5=7 < 10) -> BLOCKED,
// no-qualifying-candidate, active stays account 1.
func TestDecide_GoldenHysteresisMarginBlocks(t *testing.T) {
	pol := Policy{ThresholdPct: 90, HysteresisPct: 10, CooldownSeconds: 300, UnhealthyTicks: 3}
	in := Input{
		Accounts: []provider.AccountRow{
			acct(1, "a@x", 95, ""),
			acct(2, "b@x", 86, ""),
			acct(3, "c@x", 88, ""),
		},
		ActiveNumber: 1, Strategy: StrategyBest, Policy: pol, Now: t0,
	}
	d, err := Decide(in)
	mustBlocked(t, d, err, "no-qualifying-candidate")
}

// TestDecide_GoldenIssue115StrictlyBetterCandidateSwitches replicates
// tsamx/tests/test_autoswitch.py::test_issue_115_strictly_better_candidate_switches
// (same defaults): active bound by five_hour 99% (headroom 1); candidate 2
// bound by seven_day 89% (headroom 11, clears 1+10=11 exactly — equality
// passes per the hysteresis boundary rule); candidate 3 at five_hour 95%
// (headroom 5, 5-1=4 < 10, rejected). Switches to account 2.
func TestDecide_GoldenIssue115StrictlyBetterCandidateSwitches(t *testing.T) {
	pol := Policy{ThresholdPct: 90, HysteresisPct: 10, CooldownSeconds: 300, UnhealthyTicks: 3}
	in := Input{
		Accounts: []provider.AccountRow{
			{Number: 1, Email: "a@x", UsageStatus: "ok", Usage: &provider.Usage{
				FiveHour: &provider.Window{Pct: 99.0}, SevenDay: &provider.Window{Pct: 24.0},
			}},
			{Number: 2, Email: "b@x", UsageStatus: "ok", Usage: &provider.Usage{
				FiveHour: &provider.Window{Pct: 3.0}, SevenDay: &provider.Window{Pct: 89.0},
			}},
			{Number: 3, Email: "c@x", UsageStatus: "ok", Usage: &provider.Usage{
				FiveHour: &provider.Window{Pct: 95.0}, SevenDay: &provider.Window{Pct: 10.0},
			}},
		},
		ActiveNumber: 1, Strategy: StrategyBest, Policy: pol, Now: t0,
	}
	d, err := Decide(in)
	mustSwitch(t, d, err, 2)
	if d.Trigger != "proactive" {
		t.Fatalf("trigger = %q, want proactive", d.Trigger)
	}
}
