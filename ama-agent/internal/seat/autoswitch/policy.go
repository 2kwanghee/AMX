package autoswitch

// Strategy selects how Decide picks a switch target once the threshold gate
// and cooldown clear the way for a switch. The two literals are contract
// C1's `switch --strategy best|next-available` values (docs/design-notes/
// tsamx-rewrite-feasibility.md "재작성 시 반드시 지켜야 할 계약" table, row
// "CLI 동사") — see the package doc for why Decide reinterprets them as
// engine strategies rather than the manual-switch CLI strategies they
// originate from.
type Strategy string

const (
	StrategyBest          Strategy = "best"
	StrategyNextAvailable Strategy = "next-available"
)

// Policy is the three-value anti-flap configuration tsamx calls
// AutoSwitchSettings.threshold/hysteresis_pct/cooldown_seconds
// (tsamx/src/tsamx/settings.py), ported field-for-field:
//
//   - ThresholdPct: the active account's binding-window utilization (100 -
//     headroom) that triggers a switch consideration (autoswitch.py:943-964).
//   - HysteresisPct: how many percentage points of headroom a "best" target
//     must beat the active account by, so two accounts hovering at the same
//     line never ping-pong (autoswitch.py:1861: `h - active_headroom <
//     hysteresis_pct` rejects; equality PASSES — see decide_test.go's
//     boundary cases).
//   - CooldownSeconds: the minimum time between proactive switches
//     (autoswitch.py:2123-2127, `< cooldown_seconds` — equality PASSES,
//     i.e. is no longer "in cooldown"). Only gates the "proactive" trigger;
//     an "at-limit" trigger (active account already at/under 0 headroom)
//     bypasses it, exactly as the module docstring says (autoswitch.py:17).
type Policy struct {
	ThresholdPct    float64
	HysteresisPct   float64
	CooldownSeconds float64
}

// The four constants below are copied verbatim (same names translated,
// same values) from tsamx/src/tsamx/autoswitch.py:82-126 — the anti-flap
// margins for the "every account above threshold" recovery escape
// (recoveryIsUseful). See that file's extensive comments (autoswitch.py:
// 129-217, _recovery_is_useful's docstring) for the measured incidents each
// one closes; they are not re-derived here.
const (
	// RecoveryHysteresisS: a candidate ranked on the recovery-time axis must
	// come back at least this many seconds sooner than the account being
	// left, so two accounts whose windows roll over close together cannot
	// trade places on measurement jitter (autoswitch.py:82-90).
	RecoveryHysteresisS = 300.0

	// RecoveryHorizonS: past this horizon a sooner reset stops being worth
	// real headroom, so ranking falls back to the headroom axis
	// (autoswitch.py:92-100).
	RecoveryHorizonS = 4 * 3600.0

	// HorizonHeadroomRatio: on the headroom axis (when recovery isn't the
	// useful axis), a candidate must offer at least this many times the
	// active account's headroom — a RATIO rather than a fixed margin, so the
	// move is one-way (autoswitch.py:102-105).
	HorizonHeadroomRatio = 2.0

	// SpentHeadroomPct: below this an account is "spent" — a headroom edge
	// under it is worth less than two poll intervals of work, so when BOTH
	// the active account and the best candidate are this low, rank by
	// soonest reset instead of headroom (autoswitch.py:121-126).
	SpentHeadroomPct = 3.0
)
