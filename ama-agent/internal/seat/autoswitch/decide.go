package autoswitch

import (
	"errors"
	"fmt"
	"sort"
	"strconv"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
	"github.com/2kwanghee/AMX/ama-agent/internal/seat/profile"
)

// Outcome is the decision's coarse verdict; ResultCode maps it (plus a
// caller-supplied error) onto the four `auto --once` exit codes (contract
// C5 / feasibility doc "종료코드" row).
type Outcome int

const (
	OutcomeNoAction Outcome = iota
	OutcomeSwitched
	OutcomeBlocked
)

// ErrNextAvailableIsManualOnly is returned by Decide when Input.Strategy is
// StrategyNextAvailable. Review C3: tsamx's tick strategy field is only ever
// "best" or "consume-first" (tsamx/src/tsamx/settings.py:47-50);
// next-available is exclusively the MANUAL `switch --strategy`/SwitchNow
// literal (internal/command/handlers.go:524-527,635-641). Decide fails
// loudly instead of silently downgrading to "best" — a caller that manages
// to feed this engine a manual-switch literal (e.g. a
// default_strategy meant for switch_now leaking into an auto-tick policy)
// has a real wiring bug, and best-effort coercion would hide it as a
// quiet behavior change (blind rotation instead of headroom ranking)
// exactly where the review flagged the risk.
var ErrNextAvailableIsManualOnly = errors.New("autoswitch: next-available is a manual switch-strategy literal, not a valid tick Strategy")

// Input is everything Decide needs, entirely caller-supplied — no file
// reads, no clock reads, no network calls (package doc, "판정 로직만
// 순수하게"). ActiveNumber identifies the currently-active account among
// Accounts by its Number field (mirrors switcher.current_account_number()).
type Input struct {
	Accounts     []provider.AccountRow
	ActiveNumber int
	Strategy     Strategy
	Policy       Policy
	Now          time.Time
	// LastSwitchAt is nil when no switch has ever happened (never in
	// cooldown). Mirrors autoswitch_state.json's lastSwitchAt
	// (autoswitch.py:2089, read back at :2123-2127).
	LastSwitchAt *time.Time
	// Quarantine is the CURRENT quarantine set, keyed by
	// profile.AccountKey(email) (review C4 — the first version keyed on the
	// pool slot Number, which does not correspond to anything
	// internal/seat/usage's store or internal/seat.Switcher key on).
	// Read by the caller before calling Decide (e.g. via ReadState); Decide
	// never reads it from disk itself.
	Quarantine map[string]QuarantineEntry
	// Fingerprints supplies each account's CURRENT refresh-token fingerprint
	// (oauth.credential_fingerprint-equivalent), keyed the same way as
	// Quarantine (profile.AccountKey(email)). An absent or empty entry means
	// "not computed this tick" and never by itself triggers ShouldRelease's
	// credentials-replaced path (see ShouldRelease's doc) — this package
	// never reads credential material (package doc); only a caller that
	// actually has the credential (the OAuth-refresh track, branch
	// feat/seat-engine-p5) can supply a real value here.
	Fingerprints map[string]string
	// ConsecutiveUnhealthyTicks is the caller-persisted count of consecutive
	// PRIOR ticks where the active account's usage was unreadable — the
	// state tsamx's self._unhealthy_ticks holds in-process across ticks
	// (autoswitch.py:647,941,999). Decide returns the updated count via
	// Decision.UnhealthyTicks for the caller to persist for the next call
	// (review C2 — this package has no memory of its own between calls).
	ConsecutiveUnhealthyTicks int
	// AssignedAccountKeys is the PolicyGuard candidate boundary (design note
	// P5: "PolicyGuard 통과분만 후보로 삼는 선택 전략", review C5) — the set
	// of profile.AccountKey values the SERVER has told this agent it may
	// activate right now, the same vocabulary internal/seat.Switcher.
	// Switch's assignedKeys parameter enforces at execution time
	// (switcher.go:99-102,127, ErrNotAssigned). When non-nil, candidate
	// selection is restricted to keys in this set; a target Decide picks
	// that later fails Switch's OWN assignedKeys check would be a real bug
	// in whatever the caller passed here — Decide filters BEFORE ranking so
	// that this cannot happen.
	//
	// A nil slice means "no restriction from this input" rather than "block
	// everything". This is deliberately safe-in-depth, not safe-only-here:
	// Switcher.Switch is the actual enforcement backstop regardless of what
	// this package decides (its own doc: "this package enforces only the
	// narrower, purely mechanical rule... no local judgment about WHY"), so
	// an unset PolicyGuard input here can waste a tick's decision on an
	// account Switch will refuse, but can never cause an unauthorized
	// activation. Once a caller is wired to a real server assignment, it
	// should always populate this field.
	AssignedAccountKeys []string
}

// Target identifies a switch endpoint with everything a caller needs to
// actually execute it: Number/Email for display and event logging,
// AccountKey (review C4) as the exact profile.AccountKey(email)-shaped
// identifier internal/seat.Switcher.Switch's targetKey and
// internal/seat/usage's store key both expect.
type Target struct {
	Number     int
	Email      string
	AccountKey string
}

func targetOf(a provider.AccountRow) *Target {
	return &Target{Number: a.Number, Email: a.Email, AccountKey: profile.AccountKey(a.Email)}
}

// Decision is Decide's full result: the verdict, the target (if switched),
// quarantine deltas the caller should persist (e.g. via WriteState), the
// updated unhealthy-tick counter to persist for the next call, and the
// ordered event stream a caller can log/forward.
type Decision struct {
	Outcome Outcome
	// Trigger is "proactive" | "at-limit" | "consume-first" | "failover"
	// once the threshold/unhealthy-ticks gate has produced a verdict; ""
	// before that (no-active-account).
	Trigger string
	Reason  string
	Detail  string
	From    *Target
	To      *Target
	// NewQuarantine / Released are quarantine.json deltas this tick decided
	// on; the caller merges them into the map it passed as Input.Quarantine
	// and persists via WriteState. Decide itself never writes state.
	NewQuarantine []QuarantineEntryDelta
	Released      []QuarantineEntryDelta
	// UnhealthyTicks is the updated consecutive-unreadable-active-usage
	// counter (review C2) — 0 whenever the active account's usage WAS
	// readable this tick, otherwise Input.ConsecutiveUnhealthyTicks+1. The
	// caller persists this and passes it back as
	// Input.ConsecutiveUnhealthyTicks on the next call.
	UnhealthyTicks int
	Events         []Event
}

// QuarantineEntryDelta names one slot Decide wants added to or removed from
// the quarantine set this tick, keyed by AccountKey (review C4).
type QuarantineEntryDelta struct {
	AccountKey string
	Number     int
	Email      string
	Reason     string
}

// ResultCode maps a Decision to the `auto --once` exit-code contract
// (contract C5; internal/tsamx/contract_test.go's TestContractAutoOnceExit
// Codes pins the same four values 0/1/2/3 for ExecBridge — this mapping must
// never diverge, so a native engine wired to the same call sites is a
// drop-in). Decide itself never fails "with an error" for a normal verdict —
// pass a caller-side error (e.g. the quarantine state file could not be
// read BEFORE Decide ran, or Decide itself returned a validation error such
// as ErrNextAvailableIsManualOnly) to get CodeError; a nil error with a
// NoAction/Switched/Blocked Outcome maps to 2/0/3 respectively.
type ResultCode int

const (
	CodeSwitched ResultCode = 0
	CodeError    ResultCode = 1
	CodeNoAction ResultCode = 2
	CodeBlocked  ResultCode = 3
)

func (d Decision) ResultCode(err error) ResultCode {
	if err != nil {
		return CodeError
	}
	switch d.Outcome {
	case OutcomeSwitched:
		return CodeSwitched
	case OutcomeBlocked:
		return CodeBlocked
	default:
		return CodeNoAction
	}
}

func ref(a provider.AccountRow) *AccountRef {
	return &AccountRef{Number: a.Number, Email: a.Email, AccountKey: profile.AccountKey(a.Email)}
}

// Decide evaluates one tick: quarantine sweep, threshold/unhealthy-ticks
// gate, cooldown, PolicyGuard filtering, candidate selection under
// Input.Strategy. Pure — same input always yields the same output
// (decide_test.go pins representative cases by hand).
func Decide(in Input) (Decision, error) {
	if in.Strategy == StrategyNextAvailable {
		return Decision{}, ErrNextAvailableIsManualOnly
	}
	if in.Strategy != StrategyBest && in.Strategy != StrategyConsumeFirst {
		return Decision{}, fmt.Errorf("autoswitch: unknown strategy %q", in.Strategy)
	}

	d := Decision{}

	activeIdx := -1
	for i, a := range in.Accounts {
		if a.Number == in.ActiveNumber {
			activeIdx = i
			break
		}
	}
	if activeIdx < 0 {
		d.Outcome = OutcomeNoAction
		d.Reason = "no-active-account"
		d.Events = append(d.Events, NoSwitchEvent{Ts: in.Now, Reason: d.Reason})
		return d, nil
	}
	active := in.Accounts[activeIdx]

	// -- account key index (review C4: keyed on profile.AccountKey(email),
	// not the pool slot Number the first version used) -------------------
	byKey := make(map[string]provider.AccountRow, len(in.Accounts))
	for _, a := range in.Accounts {
		byKey[profile.AccountKey(a.Email)] = a
	}

	// -- quarantine release sweep (ShouldRelease) -------------------------
	// Ported from _release_recovered_quarantines (autoswitch.py:707-741):
	// iterates the CURRENT quarantine set (not the candidate list), exactly
	// like the original. Review C1 fix: release is fingerprint/account-
	// replaced ONLY — never merely because usageStatus stopped reporting
	// relogin_required (see ShouldRelease's doc for why the first version's
	// status-based release was a real defect, not a simplification).
	quarantineNow := make(map[string]QuarantineEntry, len(in.Quarantine))
	for k, v := range in.Quarantine {
		quarantineNow[k] = v
	}
	for key, entry := range in.Quarantine {
		row, present := byKey[key]
		var email, fingerprint string
		if present {
			email = row.Email
			fingerprint = in.Fingerprints[key]
		}
		if release, reason := ShouldRelease(entry, email, present, fingerprint); release {
			delete(quarantineNow, key)
			num := 0
			if present {
				num = row.Number
			}
			d.Released = append(d.Released, QuarantineEntryDelta{AccountKey: key, Number: num, Email: entry.Email, Reason: reason})
			d.Events = append(d.Events, UnquarantineEvent{Ts: in.Now, Number: num, AccountKey: key, Email: entry.Email, Reason: reason})
		}
	}

	// -- new quarantine sweep (ShouldQuarantine) ---------------------------
	// Ported eagerly here (see package doc) rather than lazily at
	// freshen-failure time like tsamx, since this package has no freshen
	// step to fail. The active account is never swept — a switch off it is
	// how a dead active account gets discovered (via the unhealthy-ticks
	// failover below), quarantining it before that happens would strand the
	// caller with no active account at all, which tsamx's own tick() also
	// never does (it only quarantines candidates, autoswitch.py:1303-1312).
	for _, a := range in.Accounts {
		if a.Number == in.ActiveNumber {
			continue
		}
		key := profile.AccountKey(a.Email)
		if _, already := quarantineNow[key]; already {
			continue
		}
		if ShouldQuarantine(a.UsageStatus) {
			entry := QuarantineEntry{Email: a.Email, Reason: "relogin_required", At: in.Now, RefreshTokenFingerprint: in.Fingerprints[key]}
			quarantineNow[key] = entry
			d.NewQuarantine = append(d.NewQuarantine, QuarantineEntryDelta{AccountKey: key, Number: a.Number, Email: a.Email, Reason: "relogin_required"})
			d.Events = append(d.Events, QuarantineEvent{Ts: in.Now, Number: a.Number, AccountKey: key, Email: a.Email, Reason: "relogin_required"})
		}
	}

	// -- poll event (headroom snapshot over every account) --------------
	headroomAll := make(map[string]*float64, len(in.Accounts))
	for _, a := range in.Accounts {
		headroomAll[strconv.Itoa(a.Number)] = accountHeadroom(a.Usage)
	}
	d.Events = append(d.Events, PollEvent{
		Ts:           in.Now,
		Active:       ref(active),
		HeadroomPct:  headroomAll,
		ThresholdPct: in.Policy.ThresholdPct,
	})

	// -- threshold / unhealthy-ticks gate -----------------------------------
	activeHeadroom := accountHeadroom(active.Usage)
	var trigger string
	if activeHeadroom != nil {
		// Active usage IS readable this tick: reset the failover counter
		// (autoswitch.py:941 `self._unhealthy_ticks = 0`).
		d.UnhealthyTicks = 0
		utilization := 100.0 - *activeHeadroom
		if utilization < in.Policy.ThresholdPct {
			if in.Strategy != StrategyConsumeFirst {
				d.Outcome = OutcomeNoAction
				d.Reason = "below-threshold"
				d.Detail = fmt.Sprintf("%s%% < %s%%", pctLabel(utilization), pctLabel(in.Policy.ThresholdPct))
				d.Events = append(d.Events, NoSwitchEvent{Ts: in.Now, Reason: d.Reason, Detail: d.Detail})
				return d, nil
			}
			// consume-first proactively considers a move even below
			// threshold (autoswitch.py:958-962) — trigger name changes, but
			// the threshold gate itself is bypassed for this strategy.
			trigger = "consume-first"
		} else {
			trigger = "proactive"
			if *activeHeadroom <= 0 {
				trigger = "at-limit"
			}
		}
	} else {
		// Active usage is UNREADABLE this tick (review C2 — the first
		// version treated this as a permanent NoAction dead end).
		// Ports autoswitch.py:999-1011's unhealthy-ticks counter: only after
		// UnhealthyTicks consecutive unreadable ticks does the engine fail
		// over to any healthy candidate. The idle-hold nuance
		// (autoswitch.py:966-996, USAGE_TOKEN_EXPIRED gets its own elapsed-
		// time-bounded hold before counting) is NOT ported — see package
		// doc; omitting it only makes failover trigger SOONER in the
		// idle-but-healthy case, never later, so it cannot reproduce the
		// "frozen forever" defect C2 flagged, only trade a slower cadence
		// for a simpler one.
		ticks := in.ConsecutiveUnhealthyTicks + 1
		d.UnhealthyTicks = ticks
		if ticks < in.Policy.UnhealthyTicks {
			d.Outcome = OutcomeNoAction
			d.Reason = "active-usage-unknown"
			d.Detail = fmt.Sprintf("%d/%d before failover", ticks, in.Policy.UnhealthyTicks)
			d.Events = append(d.Events, NoSwitchEvent{Ts: in.Now, Reason: d.Reason, Detail: d.Detail})
			return d, nil
		}
		trigger = "failover"
	}
	d.Trigger = trigger

	// -- cooldown (proactive/consume-first only; at-limit/failover bypass,
	// matching autoswitch.py:1013 `if trigger in ("proactive",
	// "consume-first")` and the module docstring's "bypassed only when the
	// active account is hard at its limit") --------------------------------
	if (trigger == "proactive" || trigger == "consume-first") && inCooldown(in.Now, in.LastSwitchAt, in.Policy.CooldownSeconds) {
		d.Outcome = OutcomeNoAction
		d.Reason = "cooldown"
		d.Events = append(d.Events, NoSwitchEvent{Ts: in.Now, Reason: d.Reason})
		return d, nil
	}

	// -- candidate pool: exclude active, disabled (literal C4), quarantined,
	// and (PolicyGuard, review C5) anything outside AssignedAccountKeys ----
	var assigned map[string]bool
	if in.AssignedAccountKeys != nil {
		assigned = make(map[string]bool, len(in.AssignedAccountKeys))
		for _, k := range in.AssignedAccountKeys {
			assigned[k] = true
		}
	}
	var candidates []provider.AccountRow
	for _, a := range in.Accounts {
		if a.Number == in.ActiveNumber || a.Disabled {
			continue
		}
		key := profile.AccountKey(a.Email)
		if _, q := quarantineNow[key]; q {
			continue
		}
		if assigned != nil && !assigned[key] {
			continue
		}
		candidates = append(candidates, a)
	}
	sort.Slice(candidates, func(i, j int) bool { return candidates[i].Number < candidates[j].Number })

	if len(candidates) == 0 {
		d.Outcome = OutcomeBlocked
		d.Reason = "no-candidates"
		d.Events = append(d.Events, NoSwitchEvent{Ts: in.Now, Reason: d.Reason})
		return d, nil
	}

	// activeHeadroom is nil exactly when trigger=="failover" (the active
	// account's own usage was unreadable — that's why we're failing over).
	// selectCandidate only reads its activeHeadroom argument on the GATED
	// paths (trigger "proactive"/"consume-first"), never on the "failover"
	// escape path, so 0 here is an unused placeholder, not a silent
	// "headroom 0" claim about the active account.
	activeHeadroomArg := 0.0
	if activeHeadroom != nil {
		activeHeadroomArg = *activeHeadroom
	}
	target, reason, detail := selectCandidate(active, activeHeadroomArg, candidates, in.Policy, in.Now, trigger, in.Strategy)
	d.Reason, d.Detail = reason, detail

	if target == nil {
		// "no-comparison" (no candidate had ANY readable usage) is BLOCKED
		// unconditionally, checked before the trigger-specific branches
		// below — matches autoswitch.py's ordering exactly (the
		// `if not any_known` check runs before `if trigger ==
		// "consume-first"`, autoswitch.py:1190-1200 precedes :1201).
		if reason == "no-comparison" {
			d.Outcome = OutcomeBlocked
			d.Events = append(d.Events, NoSwitchEvent{Ts: in.Now, Reason: d.Reason, Detail: d.Detail})
			return d, nil
		}
		if trigger == "consume-first" {
			d.Outcome = OutcomeNoAction
			d.Events = append(d.Events, NoSwitchEvent{Ts: in.Now, Reason: d.Reason, Detail: d.Detail})
			return d, nil
		}
		d.Outcome = OutcomeBlocked
		if d.Reason == "all-exhausted" {
			var earliest *time.Time
			if ts := earliestRecoveryTs(active, candidates, in.Now); ts != nil {
				earliest = ts
			}
			d.Events = append(d.Events, AllExhaustedEvent{Ts: in.Now, EarliestResetAt: earliest})
		} else {
			d.Events = append(d.Events, NoSwitchEvent{Ts: in.Now, Reason: d.Reason, Detail: d.Detail})
		}
		return d, nil
	}

	d.Outcome = OutcomeSwitched
	d.From = targetOf(active)
	d.To = targetOf(*target)
	d.Events = append(d.Events, SwitchEvent{Ts: in.Now, Trigger: trigger, From: ref(active), To: ref(*target)})
	return d, nil
}

func inCooldown(now time.Time, last *time.Time, cooldownSeconds float64) bool {
	if last == nil {
		return false
	}
	return now.Sub(*last).Seconds() < cooldownSeconds
}

// pctLabel mirrors autoswitch.py:241-248 (pct_label): both sides of a
// displayed comparison must format the same way, or a mixed formatter can
// render an impossible "85.5556% < 85.555555%". %.10g in Go matches
// Python's f"{value:.10g}" for the finite, non-huge percentages this engine
// ever formats.
func pctLabel(v float64) string {
	return strconv.FormatFloat(v, 'g', 10, 64)
}
