package autoswitch

import (
	"math"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
)

// accountHeadroom is the Go port of tsamx's oauth.account_headroom for the
// account-wide 5h/7d axes only (scoped per-model windows are out of scope
// for this port — see package doc). Returns nil when neither window is
// present (contract C4's "usage==null = 미측정" literal projected onto the
// fields AMA's ListResult already carries), otherwise 100 minus the higher
// of the two windows' pct — the "binding window" the module docstring
// describes (autoswitch.py:11-12).
func accountHeadroom(u *provider.Usage) *float64 {
	if u == nil {
		return nil
	}
	var maxPct float64
	known := false
	if u.FiveHour != nil {
		maxPct = u.FiveHour.Pct
		known = true
	}
	if u.SevenDay != nil && (!known || u.SevenDay.Pct > maxPct) {
		maxPct = u.SevenDay.Pct
		known = true
	}
	if !known {
		return nil
	}
	h := 100.0 - maxPct
	return &h
}

// bindingRecoveryTs is the Go port of autoswitch.py:536-567
// (_binding_recovery_ts), restricted to the 5h/7d windows accountHeadroom
// also reads (same restriction, so ranking and headroom never disagree
// about which window matters, mirroring the original's stated invariant).
// Returns +Inf (unix seconds) when unknown or already past `now`, so such
// accounts sort last rather than masquerading as "back immediately" — same
// behavior the original documents for a stale resets_at.
func bindingRecoveryTs(u *provider.Usage, now time.Time) float64 {
	if u == nil {
		return math.Inf(1)
	}
	type win struct {
		pct      float64
		resetsAt string
	}
	var wins []win
	if u.FiveHour != nil {
		wins = append(wins, win{u.FiveHour.Pct, u.FiveHour.ResetsAt})
	}
	if u.SevenDay != nil {
		wins = append(wins, win{u.SevenDay.Pct, u.SevenDay.ResetsAt})
	}
	if len(wins) == 0 {
		return math.Inf(1)
	}
	// Pick the BINDING window (max pct) first, exactly like the original —
	// filtering on the reset before the max would let a lower window win
	// whenever the binding one's reset is unknown/past (autoswitch.py's
	// comment at :556-561).
	binding := wins[0]
	for _, w := range wins[1:] {
		if w.pct > binding.pct {
			binding = w
		}
	}
	ts, err := time.Parse(time.RFC3339, binding.resetsAt)
	if err != nil || !ts.After(now) {
		return math.Inf(1)
	}
	return float64(ts.Unix())
}

// sevenDayResetTs is the Go port of autoswitch.py:514-533
// (_seven_day_reset_ts): the epoch seconds of an account's seven-day
// (weekly) window reset, or nil when unknown or already past `now`. Used
// only by StrategyConsumeFirst's ranking (review C3) — the weekly window is
// the "perishable" quota consume-first plans around; the five-hour window
// recycles too fast to be worth planning around (same rationale the
// original states).
func sevenDayResetTs(u *provider.Usage, now time.Time) *float64 {
	if u == nil || u.SevenDay == nil || u.SevenDay.ResetsAt == "" {
		return nil
	}
	ts, err := time.Parse(time.RFC3339, u.SevenDay.ResetsAt)
	if err != nil || !ts.After(now) {
		return nil
	}
	v := float64(ts.Unix())
	return &v
}

// recoveryIsUseful is a direct, unmodified port of autoswitch.py:129-216
// (_recovery_is_useful) — see that docstring for the full incident history
// this closes (47 credential rewrites over 3.9h on frozen inputs, the #202
// weekly-vs-five-hour case, and why the boundary is "measured, not argued").
// All five arguments are unix seconds / percentage points, matching the
// Python signature 1:1 (candidateRecoveryTs/activeRecoveryTs may be +Inf).
func recoveryIsUseful(candidateRecoveryTs, activeRecoveryTs, activeHeadroom, bestCandidateHeadroom, nowTs float64) bool {
	if activeHeadroom <= SpentHeadroomPct && bestCandidateHeadroom <= SpentHeadroomPct {
		return true
	}
	return candidateRecoveryTs-nowTs <= RecoveryHorizonS || activeRecoveryTs-nowTs <= RecoveryHorizonS
}
