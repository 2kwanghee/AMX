package autoswitch

import (
	"math"
	"sort"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
)

// qualified is one candidate that cleared the hysteresis/recovery gate in
// selectBest, tagged with the axis it qualified on for the tiered sort
// below.
type qualified struct {
	acct       provider.AccountRow
	headroom   float64
	byRecovery bool
	recoveryTs float64
}

// selectBest ports the "best" ranking of autoswitch.py:1773-1928
// (_rank_candidates, restricted to the "proactive"/"at-limit" triggers this
// package implements — see package doc for what's excluded: consume-first,
// the no-return bar, and the fallback re-admission list).
//
// Steps, matching the original 1:1:
//  1. Any candidate with unreadable usage is excluded from ranking but does
//     not by itself block the tick (autoswitch.py:1776-1780).
//  2. If NO candidate is readable at all -> "no-comparison" (BLOCKED, not
//     NoAction — autoswitch.py:1190-1200 already crossed the threshold gate
//     by this point).
//  3. _every_account_above_threshold (autoswitch.py:570-590): when active
//     AND every readable candidate are at/over threshold, ranking switches
//     to the recovery axis via recoveryIsUseful per candidate
//     (autoswitch.py:1793-1845).
//  4. Otherwise, plain headroom + HysteresisPct gate
//     (autoswitch.py:1857-1862).
//  5. Winner: recovery-axis qualifiers rank before headroom-axis ones
//     (tiered, autoswitch.py:1863-1928); within a tier, soonest recovery or
//     highest headroom wins; ties break on account Number ascending (this
//     package has no separate sequence.json order to fall back to, unlike
//     autoswitch.py:1877's "sequence order" — documented simplification).
//  6. No qualifier: "all-exhausted" (every candidate readable and <=0
//     headroom, autoswitch.py:1238-1240) vs "no-qualifying-candidate"
//     (autoswitch.py:1241-1252) — both BLOCKED.
func selectBest(active provider.AccountRow, activeHeadroom float64, candidates []provider.AccountRow, pol Policy, now time.Time) (*provider.AccountRow, string, string) {
	nowTs := float64(now.Unix())

	type known struct {
		acct     provider.AccountRow
		headroom float64
	}
	var all []known
	anyKnown := false
	for _, c := range candidates {
		h := accountHeadroom(c.Usage)
		if h == nil {
			continue
		}
		anyKnown = true
		all = append(all, known{c, *h})
	}
	if !anyKnown {
		return nil, "no-comparison", "no candidate has readable usage"
	}

	// _every_account_above_threshold (autoswitch.py:570-590): active is
	// already known >= threshold by construction (Decide's gate already
	// checked utilization >= ThresholdPct before calling selectBest).
	allAbove := len(all) > 0
	for _, k := range all {
		if (100.0 - k.headroom) < pol.ThresholdPct {
			allAbove = false
			break
		}
	}

	bestCandidateHeadroom := 0.0
	for _, k := range all {
		if k.headroom > bestCandidateHeadroom {
			bestCandidateHeadroom = k.headroom
		}
	}

	var activeRecoveryTs float64
	if allAbove {
		activeRecoveryTs = bindingRecoveryTs(active.Usage, now)
	}

	var qualifying []qualified
	for _, k := range all {
		if k.headroom <= 0 {
			continue // itself at its limit — never a target (autoswitch.py:1781-1782)
		}
		if allAbove {
			candRecoveryTs := bindingRecoveryTs(k.acct.Usage, now)
			byRecovery := recoveryIsUseful(candRecoveryTs, activeRecoveryTs, activeHeadroom, bestCandidateHeadroom, nowTs)
			if byRecovery {
				if candRecoveryTs >= activeRecoveryTs-RecoveryHysteresisS {
					continue
				}
				qualifying = append(qualifying, qualified{k.acct, k.headroom, true, candRecoveryTs})
			} else {
				if k.headroom < activeHeadroom*HorizonHeadroomRatio {
					continue
				}
				qualifying = append(qualifying, qualified{k.acct, k.headroom, false, candRecoveryTs})
			}
			continue
		}
		if k.headroom-activeHeadroom < pol.HysteresisPct {
			continue
		}
		qualifying = append(qualifying, qualified{k.acct, k.headroom, false, 0})
	}

	if len(qualifying) == 0 {
		truly := true
		for _, c := range candidates {
			h := accountHeadroom(c.Usage)
			if h == nil || *h > 0 {
				truly = false
				break
			}
		}
		if truly {
			return nil, "all-exhausted", ""
		}
		return nil, "no-qualifying-candidate", "no candidate is below the threshold and better than the active account by the hysteresis margin, or usage is unreadable this tick"
	}

	sort.Slice(qualifying, func(i, j int) bool {
		a, b := qualifying[i], qualifying[j]
		if a.byRecovery != b.byRecovery {
			return a.byRecovery
		}
		if a.byRecovery {
			if a.recoveryTs != b.recoveryTs {
				return a.recoveryTs < b.recoveryTs
			}
		} else if a.headroom != b.headroom {
			return a.headroom > b.headroom
		}
		return a.acct.Number < b.acct.Number
	})
	winner := qualifying[0].acct
	return &winner, "", ""
}

// selectNextAvailable ports switcher.py's `switch --strategy next-available`
// rotation (switcher.py:4617-4722): starting after the active account's
// slot, wrap through candidates in ascending Number order, skip any account
// whose headroom is KNOWN and <=0 (switcher.py:4663-4666 — an UNKNOWN
// headroom is NOT skipped, unlike selectBest, matching the original's
// asymmetry: next-available only proves exhaustion, it never requires
// proof of health).
func selectNextAvailable(activeNumber int, candidates []provider.AccountRow) (*provider.AccountRow, string) {
	ordered := rotateFrom(candidates, activeNumber)
	skippedExhausted := false
	for _, c := range ordered {
		h := accountHeadroom(c.Usage)
		if h != nil && *h <= 0 {
			skippedExhausted = true
			continue
		}
		cc := c
		return &cc, ""
	}
	if skippedExhausted {
		return nil, "candidates-exhausted"
	}
	return nil, "no-valid-target"
}

// rotateFrom orders candidates (already ascending by Number) as "next after
// activeNumber, wrapping around" — the same rotation switcher.py computes
// via `(current_index+offset) % len(sequence)` (switcher.py:4643-4644),
// expressed directly on candidate Numbers since this package receives a
// snapshot rather than tsamx's persisted sequence.json slot order.
func rotateFrom(candidates []provider.AccountRow, activeNumber int) []provider.AccountRow {
	var after, before []provider.AccountRow
	for _, c := range candidates {
		if c.Number > activeNumber {
			after = append(after, c)
		} else {
			before = append(before, c)
		}
	}
	return append(after, before...)
}

// earliestRecoveryTs is a narrowed port of autoswitch.py:2173-2207
// (_earliest_recovery): the soonest moment any exhausted account (active or
// candidate) becomes usable again, or nil when that can't be proven
// (unknown reset on some exhausted row). Narrowed to the 5h/7d windows
// accountHeadroom/bindingRecoveryTs already read (scoped per-model windows
// out of scope, see package doc) — informational only (AllExhaustedEvent's
// earliestResetAt), never a ranking input.
func earliestRecoveryTs(active provider.AccountRow, candidates []provider.AccountRow, now time.Time) *time.Time {
	all := append([]provider.AccountRow{active}, candidates...)
	var earliest *time.Time
	for _, a := range all {
		h := accountHeadroom(a.Usage)
		if h == nil || *h > 0 {
			continue // not exhausted, doesn't gate the blocked state
		}
		ts := bindingRecoveryTs(a.Usage, now)
		if math.IsInf(ts, 1) {
			return nil // exhausted with unprovable recovery -> whole answer unprovable
		}
		t := time.Unix(int64(ts), 0).UTC()
		if earliest == nil || t.Before(*earliest) {
			earliest = &t
		}
	}
	return earliest
}
