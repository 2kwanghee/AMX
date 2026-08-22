package autoswitch

import (
	"testing"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
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

func acct(number int, email string, pct float64, resetsAt string) provider.AccountRow {
	return provider.AccountRow{Number: number, Email: email, Usage: usage(pct, resetsAt), UsageStatus: "ok"}
}

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
	pol := Policy{ThresholdPct: 90, HysteresisPct: 10, CooldownSeconds: 300}
	// utilization 89.999 < 90 -> below-threshold (strict <, autoswitch.py:944).
	below := Input{
		Accounts:     []provider.AccountRow{acct(1, "a@x", 89.999, ""), acct(2, "b@x", 0, "")},
		ActiveNumber: 1, Strategy: StrategyBest, Policy: pol, Now: t0,
	}
	d, err := Decide(below)
	mustNoAction(t, d, err, "below-threshold")

	// utilization == 90 -> triggers (not "< threshold"), and account 2 has
	// enough headroom (100) to clear the hysteresis margin, so it switches.
	at := below
	at.Accounts = []provider.AccountRow{acct(1, "a@x", 90.0, ""), acct(2, "b@x", 0, "")}
	d, err = Decide(at)
	mustSwitch(t, d, err, 2)
}

// --- 안티 플랩 1: hysteresis_pct 경계 (< vs ==) --------------------------

func TestDecide_HysteresisBoundary(t *testing.T) {
	pol := Policy{ThresholdPct: 90, HysteresisPct: 10, CooldownSeconds: 300}
	// active headroom = 100-95 = 5. Candidate headroom must be >= 5+10 = 15
	// to qualify (autoswitch.py:1861: `h - active_headroom < hysteresis_pct`
	// rejects; NOT rejecting on equality means qualifying at exactly 15).
	// Neither account is all-above-threshold here (candidate utilization
	// 85 < 90), so this exercises the plain hysteresis gate, not the
	// recovery escape.
	qualifies := Input{
		Accounts:     []provider.AccountRow{acct(1, "a@x", 95, ""), acct(2, "b@x", 85, "")}, // headroom 15
		ActiveNumber: 1, Strategy: StrategyBest, Policy: pol, Now: t0,
	}
	d, err := Decide(qualifies)
	mustSwitch(t, d, err, 2)

	// headroom 14.999 (one thousandth under the margin) -> rejected.
	rejects := qualifies
	rejects.Accounts = []provider.AccountRow{acct(1, "a@x", 95, ""), acct(2, "b@x", 85.001, "")}
	d, err = Decide(rejects)
	mustBlocked(t, d, err, "no-qualifying-candidate")
}

// --- 안티 플랩 2: cooldown_seconds 경계 (< vs ==) -------------------------

func TestDecide_CooldownBoundary(t *testing.T) {
	pol := Policy{ThresholdPct: 90, HysteresisPct: 10, CooldownSeconds: 120}
	accounts := []provider.AccountRow{acct(1, "a@x", 95, ""), acct(2, "b@x", 80, "")} // headroom 20, clears margin easily
	// exactly 120s since last switch: `(now-last) < cooldown` is false at
	// equality (autoswitch.py:2123-2127) -> cooldown has ended, switch allowed.
	last := t0.Add(-120 * time.Second)
	in := Input{Accounts: accounts, ActiveNumber: 1, Strategy: StrategyBest, Policy: pol, Now: t0, LastSwitchAt: &last}
	d, err := Decide(in)
	mustSwitch(t, d, err, 2)

	// 119s since last switch: still in cooldown -> NoAction.
	last2 := t0.Add(-119 * time.Second)
	in2 := in
	in2.LastSwitchAt = &last2
	d, err = Decide(in2)
	mustNoAction(t, d, err, "cooldown")

	// at-limit (headroom<=0) bypasses cooldown entirely (module docstring
	// autoswitch.py:17, "bypassed only when the active account is hard at
	// its limit").
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
	pol := Policy{ThresholdPct: 90, HysteresisPct: 10, CooldownSeconds: 0}
	// Both active and the candidate are ABOVE the threshold (headroom 5 each,
	// utilization 95 >= 90) -> all_above engages, ranking switches to the
	// recovery axis (autoswitch.py:1793-1845). Active's five_hour resets in
	// 10h (outside the 4h RecoveryHorizonS); candidate's resets in 2h
	// (inside it) -> recoveryIsUseful is true for the candidate via the
	// horizon clause alone (headroom 5 > SpentHeadroomPct 3, so the "both
	// spent" branch does not apply).
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

	// Recovery hysteresis boundary: candidate must clear
	// activeRecoveryTs-300s STRICTLY (autoswitch.py:1829: `recovery_ts >=
	// active_recovery_ts - RECOVERY_HYSTERESIS_S` rejects on >=). Pick an
	// active reset 10000s out and a candidate reset exactly 300s sooner
	// (9700s out) -> rejected (boundary is inclusive against the candidate).
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

	// One second sooner clears the margin -> switches.
	candAtPass := t0.Add(9699 * time.Second).Format(time.RFC3339)
	pass := boundary
	pass.Accounts = []provider.AccountRow{
		acct(1, "a@x", 95, activeAt),
		acct(2, "b@x", 95, candAtPass),
	}
	d, err = Decide(pass)
	mustSwitch(t, d, err, 2)

	// Truly exhausted (every account, active included, at headroom<=0) ->
	// BLOCKED "all-exhausted" -> ResultCode 3 (전원 소진 시 코드 3).
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

// --- 격리: relogin_required는 격리, token_expired는 격리 아님 ------------

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

func TestDecide_QuarantineSweep(t *testing.T) {
	pol := Policy{ThresholdPct: 90, HysteresisPct: 10, CooldownSeconds: 0}
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
	if len(d.NewQuarantine) != 1 || d.NewQuarantine[0].Number != "2" {
		t.Fatalf("NewQuarantine = %+v, want exactly slot 2", d.NewQuarantine)
	}
	if len(d.Released) != 0 {
		t.Fatalf("Released = %+v, want none", d.Released)
	}

	// Release: slot 2 was quarantined last tick, but its status has since
	// recovered to "ok" -> released and eligible again.
	recovered := acct(2, "dead@x", 30, "") // headroom 70, best target now
	in2 := Input{
		Accounts:     []provider.AccountRow{acct(1, "a@x", 95, ""), recovered, expired, healthy},
		ActiveNumber: 1, Strategy: StrategyBest, Policy: pol, Now: t0,
		Quarantine: map[string]QuarantineEntry{"2": {Email: "dead@x"}},
	}
	d, err = Decide(in2)
	mustSwitch(t, d, err, 2) // 70 headroom beats healthy's 20
	if len(d.Released) != 1 || d.Released[0].Number != "2" {
		t.Fatalf("Released = %+v, want exactly slot 2", d.Released)
	}
}

// --- 선택 전략 결정성(동점 처리) ------------------------------------------

func TestDecide_StrategyDeterminism(t *testing.T) {
	pol := Policy{ThresholdPct: 90, HysteresisPct: 10, CooldownSeconds: 0}
	// Two candidates tied on headroom (both 40) -> "best" must deterministically
	// pick the lower account Number every time (decide.go's sort tie-break).
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

	// next-available: rotation order (skip active, wrap), ignores headroom
	// entirely except to skip proven-exhausted slots -> always account 2
	// (the next slot after 1), even though 3 has identical headroom.
	tie.Strategy = StrategyNextAvailable
	for i := 0; i < 5; i++ {
		d, err := Decide(tie)
		mustSwitch(t, d, err, 2)
	}

	// next-available skips a proven-exhausted next slot and wraps to the
	// next non-exhausted one.
	exhaustedNext := Input{
		Accounts: []provider.AccountRow{
			acct(1, "a@x", 95, ""),
			acct(2, "b@x", 100, ""), // headroom 0 -> skipped
			acct(3, "c@x", 60, ""),
		},
		ActiveNumber: 1, Strategy: StrategyNextAvailable, Policy: pol, Now: t0,
	}
	d, err := Decide(exhaustedNext)
	mustSwitch(t, d, err, 3)
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
	pol := Policy{ThresholdPct: 90, HysteresisPct: 10, CooldownSeconds: 300}
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
	pol := Policy{ThresholdPct: 90, HysteresisPct: 10, CooldownSeconds: 300}
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
