package autoswitch

import (
	"math"
	"sort"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
)

// ranked is one candidate that survived filtering, carrying a (tier,
// primary, secondary) sort key. Ascending sort on (tier, primary,
// secondary, Number) reproduces tsamx's tuple-key `.sort()` exactly —
// Python tuple comparison is lexicographic, which Go's multi-field
// less-than replicates field by field.
type ranked struct {
	acct               provider.AccountRow
	tier               int
	primary, secondary float64
}

// selectCandidate ports the ranking half of autoswitch.py's tick — the
// filtering block at autoswitch.py:1773-1862 and the sort-key block at
// autoswitch.py:1863-1898 (_rank_candidates), INCLUDING the headroom-axis
// fallback re-admission list (autoswitch.py:1838-1845,1896 — review N3) —
// restricted to the two strategies and two gated triggers this package
// implements (see package doc for what remains out of scope: the
// consume-first two-phase commit, the "no-return account" bar, and API-key
// candidates).
//
// trigger is "proactive" | "at-limit" | "consume-first" | "failover"
// (decide.go). strategy is StrategyBest or StrategyConsumeFirst (Decide
// rejects anything else before calling this). The two axes are
// independent, matching the original's independent `if trigger in (...)`
// (filtering) and `if all_above and trigger in (...) / elif consume_first`
// (sort key) chains:
//
//   - "proactive"/"consume-first" triggers are GATED: a candidate must not
//     itself land at/over threshold (landing check, skipped when
//     all_above), and then either the all_above recovery escape, the
//     consume-first reset-ordering, or the best hysteresis gate applies.
//   - "at-limit"/"failover" triggers are an ESCAPE: any candidate with
//     headroom>0 qualifies outright (autoswitch.py's comment at :1795-1797,
//     "any account with real headroom beats a blocked or dead one") — but
//     the SORT ORDER among qualifiers still honors StrategyConsumeFirst
//     (autoswitch.py:1888's `elif consume_first` is NOT itself
//     trigger-gated, so even an escape ranks by soonest weekly reset under
//     that strategy).
func selectCandidate(active provider.AccountRow, activeHeadroom float64, candidates []provider.AccountRow, pol Policy, now time.Time, trigger string, strategy Strategy) (*provider.AccountRow, string, string) {
	nowTs := float64(now.Unix())
	consumeFirst := strategy == StrategyConsumeFirst

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

	gated := trigger == "proactive" || trigger == "consume-first"

	var allAbove bool
	var bestCandidateHeadroom, activeRecoveryTs float64
	var activeResetTs *float64
	if gated {
		// _every_account_above_threshold (autoswitch.py:570-590). The active
		// account is already known >= threshold whenever trigger=="proactive"
		// (Decide's gate ensured it); for trigger=="consume-first" the active
		// may be BELOW threshold, in which case allAbove is force-false below
		// (matches the original: active_headroom's own utilization must be
		// >= threshold for allAbove to ever be true).
		allAbove = (100.0-activeHeadroom) >= pol.ThresholdPct && len(all) > 0
		for _, k := range all {
			if (100.0 - k.headroom) < pol.ThresholdPct {
				allAbove = false
			}
			if k.headroom > bestCandidateHeadroom {
				bestCandidateHeadroom = k.headroom
			}
		}
		if allAbove {
			activeRecoveryTs = bindingRecoveryTs(active.Usage, now)
		}
		if consumeFirst {
			activeResetTs = sevenDayResetTs(active.Usage, now)
		}
	}

	// fallback (review N3, autoswitch.py:1838-1845): candidates that failed
	// the headroom-ratio gate below but still meet a narrower re-admission
	// test. Only consulted if `qualifying` ends up completely empty
	// (autoswitch.py:1896 `qualifying = qualifying or fallback`).
	var qualifying, fallback []ranked
	for _, k := range all {
		if k.headroom <= 0 {
			continue // itself at its limit — never a target (autoswitch.py:1781-1782)
		}

		if !gated {
			// Escape (at-limit/failover): unconditional qualification, sort
			// order alone depends on strategy (autoswitch.py:1888,1893).
			if consumeFirst {
				rt := math.Inf(1)
				if ts := sevenDayResetTs(k.acct.Usage, now); ts != nil {
					rt = *ts
				}
				qualifying = append(qualifying, ranked{k.acct, 0, rt, -k.headroom})
			} else {
				qualifying = append(qualifying, ranked{k.acct, 0, -k.headroom, 0})
			}
			continue
		}

		// Gated (proactive/consume-first trigger): landing check first
		// (autoswitch.py:1798), void whenever allAbove (comment at :1799-1804).
		if (100.0-k.headroom) >= pol.ThresholdPct && !allAbove {
			continue
		}
		switch {
		case allAbove:
			candRecoveryTs := bindingRecoveryTs(k.acct.Usage, now)
			byRecovery := recoveryIsUseful(candRecoveryTs, activeRecoveryTs, activeHeadroom, bestCandidateHeadroom, nowTs)
			if byRecovery {
				if candRecoveryTs >= activeRecoveryTs-RecoveryHysteresisS {
					continue
				}
				qualifying = append(qualifying, ranked{k.acct, 0, candRecoveryTs, -k.headroom})
			} else {
				if k.headroom < activeHeadroom*HorizonHeadroomRatio {
					// Rejected by the headroom-ratio gate, but review N3's
					// fallback re-admission (autoswitch.py:1838-1845) still
					// applies: active is SPENT (<=SpentHeadroomPct), this
					// candidate is at least as good headroom-wise, and it
					// recovers meaningfully sooner than the active account
					// (the same RecoveryHysteresisS margin the recovery axis
					// itself uses). Appended, not qualified — only used if
					// `qualifying` ends up empty (see below the loop).
					if activeHeadroom <= SpentHeadroomPct &&
						k.headroom >= activeHeadroom &&
						candRecoveryTs < activeRecoveryTs-RecoveryHysteresisS {
						fallback = append(fallback, ranked{k.acct, 0, candRecoveryTs, -k.headroom})
					}
					continue
				}
				qualifying = append(qualifying, ranked{k.acct, 1, -k.headroom, candRecoveryTs})
			}
		case consumeFirst:
			// Review N1: the reset filter itself is gated on the TRIGGER
			// STRING being literally "consume-first" (autoswitch.py:1843
			// `if trigger == "consume-first" and (...)`), NOT on the
			// strategy alone. When trigger=="proactive" (active crossed
			// threshold) under StrategyConsumeFirst, this filter is
			// skipped entirely — the landing check above is the only gate,
			// and the candidate qualifies unconditionally, ranked by reset
			// order (unknown resets sort last via +Inf, autoswitch.py:1891
			// `reset_ts if reset_ts is not None else float("inf")`).
			candResetTs := sevenDayResetTs(k.acct.Usage, now)
			if trigger == "consume-first" {
				if candResetTs == nil || activeResetTs == nil || *candResetTs >= *activeResetTs {
					continue
				}
			}
			rt := math.Inf(1)
			if candResetTs != nil {
				rt = *candResetTs
			}
			qualifying = append(qualifying, ranked{k.acct, 0, rt, -k.headroom})
		default: // best, not all_above
			if k.headroom-activeHeadroom < pol.HysteresisPct {
				continue
			}
			qualifying = append(qualifying, ranked{k.acct, 0, -k.headroom, 0})
		}
	}

	if len(qualifying) == 0 && len(fallback) > 0 {
		// autoswitch.py:1896 `qualifying = qualifying or fallback`.
		qualifying = fallback
	}

	if len(qualifying) == 0 {
		// trigger=="consume-first" reasons are NoAction-shaped, distinct
		// from the BLOCKED reasons below — this distinction is keyed on the
		// TRIGGER STRING, not the strategy (autoswitch.py:1201-1230): a
		// below-threshold consume-first nudge finding nothing is healthy
		// (stay put), while an above-threshold "proactive" trigger under
		// consume-first strategy finding nothing is the same real problem
		// best-strategy has (BLOCKED).
		if trigger == "consume-first" {
			if activeResetTs == nil {
				return nil, "reset-unknown", "active account's weekly reset time is unknown; consume-first is idle until it is reported"
			}
			return nil, "already-consuming-soonest", "no sooner-resetting account with room to spare"
		}
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
		if a.tier != b.tier {
			return a.tier < b.tier
		}
		if a.primary != b.primary {
			return a.primary < b.primary
		}
		if a.secondary != b.secondary {
			return a.secondary < b.secondary
		}
		// No separate sequence.json order in this package (see package
		// doc) — account Number ascending is the deterministic tie-break.
		return a.acct.Number < b.acct.Number
	})
	winner := qualifying[0].acct
	return &winner, "", ""
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
