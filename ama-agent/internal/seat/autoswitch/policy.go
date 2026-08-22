package autoswitch

// Strategy selects how Decide ranks candidates once the threshold gate and
// cooldown clear the way for a switch. Corrected in review C3: the FIRST
// version of this file wrongly reused contract C1's manual
// `switch --strategy best|next-available` literals as tick strategies.
// tsamx's real tick-strategy field is AutoSwitchSettings.strategy
// (tsamx/src/tsamx/settings.py:47-50), a plain string documented as
// `"best" (most headroom) or "consume-first" (soonest weekly reset)` —
// next-available never appears there at all; it is exclusively the manual
// `switch --strategy` / SwitchNow default-strategy literal (internal/
// command/handlers.go:524-527,635-641, amxv1.SwitchNow_SwitchStrategy).
// Decide accepts only StrategyBest/StrategyConsumeFirst; see Decide's doc
// for what happens if StrategyNextAvailable is passed anyway.
type Strategy string

const (
	// StrategyBest ports tsamx's default "best" tick ranking: most headroom,
	// gated by HysteresisPct (see decide.go/select.go).
	StrategyBest Strategy = "best"
	// StrategyConsumeFirst ports tsamx's "consume-first" tick ranking:
	// spend the soonest-resetting weekly (7-day) quota first, moving to any
	// candidate whose seven-day window resets strictly sooner than the
	// active account's (autoswitch.py:1846-1856).
	StrategyConsumeFirst Strategy = "consume-first"
	// StrategyNextAvailable is contract C1's MANUAL switch-strategy literal
	// (`switch --strategy next-available`) — never a valid tsamx tick
	// strategy (see this type's doc). Kept as a named constant purely so a
	// caller that receives this literal from the wire (e.g. a
	// misconfigured default_strategy meant for switch_now) can recognize it
	// by name; Decide rejects it explicitly rather than silently
	// reinterpreting it (see Decide's doc, review C3).
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
	// UnhealthyTicks mirrors AutoSwitchSettings.unhealthy_ticks (settings.py:
	// 52, default 3): the number of consecutive ticks the active account's
	// usage may stay unreadable before Decide fails over to any healthy
	// candidate (trigger "failover", autoswitch.py:999-1011). Added in
	// review C2 — the first version of this package had no such counter and
	// a dead active token froze the whole pool on NoAction/exit-2 forever.
	// Callers should set this to >=1 (tsamx bounds it 1-100,
	// settings.py:131); Decide never fails over on its own if this is <=0
	// (see Decide's doc).
	UnhealthyTicks int
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

	// IdleHoldMaxS mirrors autoswitch.py:80 (`IDLE_HOLD_MAX_S = 30 * 60.0`)
	// exactly — same name shape, same value, 1800 seconds. See decide.go's
	// idle-hold handling (review N2) for how it's used: while the active
	// account's token is expired AND Claude Code is presumably just idle
	// (not actively erroring), the engine holds — does NOT count toward
	// UnhealthyTicks — for up to this long before falling back to normal
	// failover counting.
	IdleHoldMaxS = 30 * 60.0
)
