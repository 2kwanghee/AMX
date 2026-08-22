package autoswitch

import (
	"fmt"
	"sort"
	"strconv"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
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
	// Quarantine is the CURRENT quarantine set (slot number as decimal
	// string -> entry), read by the caller before calling Decide — e.g. via
	// ReadState. Decide never reads it from disk itself.
	Quarantine map[string]QuarantineEntry
}

// Decision is Decide's full result: the verdict, the target (if switched),
// quarantine deltas the caller should persist (e.g. via WriteState), and
// the ordered event stream a caller can log/forward.
type Decision struct {
	Outcome Outcome
	// Trigger is "proactive" or "at-limit" when Outcome != NoAction/before
	// the threshold gate; "" otherwise (autoswitch.py:963-964's two
	// triggers this port implements — see package doc for what's excluded).
	Trigger string
	Reason  string
	Detail  string
	From    *provider.AccountRow
	To      *provider.AccountRow
	// NewQuarantine / Released are quarantine.json deltas this tick decided
	// on; the caller merges them into the map it passed as Input.Quarantine
	// and persists via WriteState. Decide itself never writes state.
	NewQuarantine []QuarantineEntryDelta
	Released      []QuarantineEntryDelta
	Events        []Event
}

// QuarantineEntryDelta names one slot Decide wants added to or removed from
// the quarantine set this tick.
type QuarantineEntryDelta struct {
	Number string
	Email  string
	Reason string
}

// ResultCode maps a Decision to the `auto --once` exit-code contract
// (contract C5; internal/tsamx/contract_test.go's TestContractAutoOnceExit
// Codes pins the same four values 0/1/2/3 for ExecBridge — this mapping must
// never diverge, so a native engine wired to the same call sites is a
// drop-in). Decide itself never fails "with an error" — pass a caller-side
// error (e.g. the quarantine state file could not be read BEFORE Decide
// ran) to get CodeError; a nil error with a NoAction/Switched/Blocked
// Outcome maps to 2/0/3 respectively.
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
	return &AccountRef{Number: a.Number, Email: a.Email}
}

// Decide evaluates one tick: quarantine sweep, threshold gate, cooldown,
// candidate selection under Input.Strategy. Pure — same input always yields
// the same output (decide_test.go pins representative cases by hand).
func Decide(in Input) (Decision, error) {
	if in.Strategy != StrategyBest && in.Strategy != StrategyNextAvailable {
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

	// -- quarantine sweep (ShouldQuarantine/ShouldRelease) --------------
	// Ported eagerly here (see package doc) rather than lazily at
	// freshen-failure time like tsamx, since this package has no freshen
	// step to fail. The active account is never swept — a switch off it is
	// how a dead active account would be discovered, quarantining it before
	// that happens would strand the caller with no active account at all,
	// which tsamx's own tick() also never does (it only quarantines
	// candidates, autoswitch.py:1303-1312).
	quarantineNow := make(map[string]bool, len(in.Quarantine))
	for k := range in.Quarantine {
		quarantineNow[k] = true
	}
	for _, a := range in.Accounts {
		if a.Number == in.ActiveNumber {
			continue
		}
		key := strconv.Itoa(a.Number)
		_, already := in.Quarantine[key]
		switch {
		case ShouldQuarantine(a.UsageStatus) && !already:
			d.NewQuarantine = append(d.NewQuarantine, QuarantineEntryDelta{Number: key, Email: a.Email, Reason: "relogin_required"})
			d.Events = append(d.Events, QuarantineEvent{Ts: in.Now, Number: key, Email: a.Email, Reason: "relogin_required"})
			quarantineNow[key] = true
		case ShouldRelease(a.UsageStatus, already):
			d.Released = append(d.Released, QuarantineEntryDelta{Number: key, Email: a.Email, Reason: "status-recovered"})
			d.Events = append(d.Events, UnquarantineEvent{Ts: in.Now, Number: key, Email: a.Email, Reason: "status-recovered"})
			delete(quarantineNow, key)
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

	// -- threshold gate ----------------------------------------------------
	activeHeadroom := accountHeadroom(active.Usage)
	if activeHeadroom == nil {
		d.Outcome = OutcomeNoAction
		d.Reason = "active-usage-unknown"
		d.Events = append(d.Events, NoSwitchEvent{Ts: in.Now, Reason: d.Reason})
		return d, nil
	}
	utilization := 100.0 - *activeHeadroom
	if utilization < in.Policy.ThresholdPct {
		d.Outcome = OutcomeNoAction
		d.Reason = "below-threshold"
		d.Detail = fmt.Sprintf("%s%% < %s%%", pctLabel(utilization), pctLabel(in.Policy.ThresholdPct))
		d.Events = append(d.Events, NoSwitchEvent{Ts: in.Now, Reason: d.Reason, Detail: d.Detail})
		return d, nil
	}
	trigger := "proactive"
	if *activeHeadroom <= 0 {
		trigger = "at-limit"
	}
	d.Trigger = trigger

	// -- cooldown (proactive only; at-limit is the documented bypass) ------
	if trigger == "proactive" && inCooldown(in.Now, in.LastSwitchAt, in.Policy.CooldownSeconds) {
		d.Outcome = OutcomeNoAction
		d.Reason = "cooldown"
		d.Events = append(d.Events, NoSwitchEvent{Ts: in.Now, Reason: d.Reason})
		return d, nil
	}

	// -- candidate pool: exclude active, disabled (literal C4), quarantined
	var candidates []provider.AccountRow
	for _, a := range in.Accounts {
		if a.Number == in.ActiveNumber || a.Disabled {
			continue
		}
		if quarantineNow[strconv.Itoa(a.Number)] {
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

	var target *provider.AccountRow
	switch in.Strategy {
	case StrategyBest:
		target, d.Reason, d.Detail = selectBest(active, *activeHeadroom, candidates, in.Policy, in.Now)
	case StrategyNextAvailable:
		target, d.Reason = selectNextAvailable(in.ActiveNumber, candidates)
	}

	if target == nil {
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
	fromCopy := active
	toCopy := *target
	d.From = &fromCopy
	d.To = &toCopy
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
