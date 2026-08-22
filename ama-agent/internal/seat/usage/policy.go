// Package usage implements the P4 usage collector for the new seat engine
// (design note docs/design-notes/seat-engine-plan.md, §0 불변 원칙 and the P4
// section). It is DELIBERATELY INERT: nothing in cmd/ama or the existing
// tsamx bridge constructs anything from this package. The tsamx path stays
// the default and its behavior is unchanged by this package's existence.
//
// This file ports tsamx's polling cadence policy
// (tsamx/src/tsamx/poll_policy.py) to Go, constant-for-constant and
// branch-for-branch. That policy is not a guess: it is reverse-engineered
// from measured behavior of Anthropic's `/api/oauth/usage` endpoint (a
// ~60-minute sliding window of ~28-30 requests per identity, a budget that
// is SHARED across every machine polling the same account under the
// conservative account-scoped 429 regime — no machine can see another's
// requests, and the endpoint exposes no remaining-request count, only a
// Retry-After once already blocked). The AIMD backoff below is how several
// machines converge on a fair share of that budget by reaction alone, with
// no shared state or machine count to configure. See poll_policy.py's module
// docstring for the full measurement notes (probe dates, sample sizes,
// error bars) — they are not re-derived here.
//
// DO NOT change the constants below without re-running the measurement that
// justified them. A tightened value risks 429-blocking real accounts; a
// loosened one erodes the safety margin the whole design leans on.
package usage

import (
	"math"
	"math/rand"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
)

// Cadence policy constants, ported 1:1 from tsamx/src/tsamx/poll_policy.py.
// Names mirror the Python module (SCREAMING_SNAKE -> Go CamelCase); values,
// units (seconds unless noted), and relative ordering are unchanged.
const (
	// ServeTTLS is the freshness floor a caller should apply BEFORE calling
	// this package's Fetch/PlanAfterFetch at all: an observation younger than
	// this is served from whatever cache the caller keeps, with no network
	// call. This package does not own a cache, so it does not enforce this
	// itself — it is ported here so the constant lives next to the rest of
	// the policy it was measured alongside, per poll_policy.py's own note
	// ("If a future probe revises the measured shape, adjust the constants
	// in this module only").
	ServeTTLS = 180.0

	// MinIntervalS is the cadence floor: movement can halve an interval down
	// to this, never below.
	MinIntervalS = 180.0

	// UrgentIntervalS is the bounded fast cadence for the ACTIVE account when
	// it is moving toward the switch threshold (see PlanAfterFetch).
	UrgentIntervalS = 60.0

	// ActiveMaxIntervalS/CandidateDefaultIntervalS/CandidateMaxIntervalS are
	// the decay ceilings for an account whose usage is not moving: the
	// active account stays reasonably fresh, an idle candidate drifts out
	// further.
	ActiveMaxIntervalS        = 300.0
	CandidateDefaultIntervalS = 300.0
	CandidateMaxIntervalS     = 600.0

	// ExhaustedIntervalS is the floor interval once an account has hit a
	// limit (headroom <= 0): slow enough to stay under budget, fast enough
	// (six requests/hour) to notice an early provider-side quota grant.
	ExhaustedIntervalS = 600.0

	// MovementDeltaPct is the minimum binding-pct delta between two polls
	// that counts as "being consumed" rather than measurement noise.
	MovementDeltaPct = 1.0

	// JitterFrac is the ± fraction applied to every scheduled interval so
	// independent processes polling the same account drift apart instead of
	// fetching in lockstep.
	JitterFrac = 0.1

	// EdgeBackoffS is the probe ceiling for the saturated-window 429 edge
	// (Retry-After: 0): at most one probe every 5 minutes so the ~30/hour
	// aging-out of old requests outpaces the probing. This lives in tsamx's
	// usage-store failure backoff, one layer above poll_policy's own
	// plan_after_fetch — ported here as a documented constant for a future
	// caller wiring that layer; PlanAfterFetch itself does not consume it.
	EdgeBackoffS = 300.0

	// Post429MinIntervalS is the floor cadence while a 429 was seen on this
	// token within RecentWindow429S, so freed capacity accumulates instead
	// of being re-spent immediately.
	Post429MinIntervalS = 360.0

	// RecentWindow429S is how long a 429 keeps the AIMD floor/backoff active
	// — matches the measured saturation horizon (up to a full trailing hour
	// to age out).
	RecentWindow429S = 3600.0

	// Post429BackoffMult/Post429MaxIntervalS: AIMD multiplicative increase on
	// a contended, machine-invisible budget. Each successful poll while a
	// recent 429 stands grows the interval ×Post429BackoffMult toward
	// Post429MaxIntervalS — wider than the normal candidate ceiling so
	// several machines can each back off far enough that their combined rate
	// fits under the shared budget.
	Post429BackoffMult = 1.5
	Post429MaxIntervalS = 1800.0

	// EscalationMarginPct is the distance from the switch threshold at which
	// an active, moving account escalates to UrgentIntervalS.
	EscalationMarginPct = 15.0

	// ResetSlackS bounds how far past a known window reset the next poll may
	// be scheduled: never later than the reset + this slack, since a stored
	// usage measurement is obsolete the moment the window rolls over.
	ResetSlackS = 60.0
)

// fiveHourWindowMinutes/sevenDayWindowMinutes are the fixed window widths for
// Claude's two always-present usage windows, used by the collector (not the
// scheduler) to fill provider.Window.WindowMinutes.
const (
	fiveHourWindowMinutes = 5 * 60
	sevenDayWindowMinutes = 7 * 24 * 60
)

// relevantWindow is one (label, pct, resetsAt) window that gates an account,
// mirroring tsamx.oauth.relevant_windows's tuple shape. resetsAt is the raw
// string as fetched (parsed lazily by ParseResetTS), or "" when the API sent
// none.
type relevantWindow struct {
	Label    string
	Pct      float64
	ResetsAt string
}

// RelevantWindows returns every window that gates usage.Usage: the five_hour
// ("5h") and seven_day ("7d") windows when present, plus — when models is
// non-empty — each named per-model scoped weekly window (matched
// case-insensitively on display name; the sentinel "all" matches every
// scoped window the account reports). This is the single canonical window
// source for BindingPct/AccountHeadroom/LimitingResetTS/EarliestFutureResetTS
// below, ported from tsamx.oauth.relevant_windows.
func RelevantWindows(u *provider.Usage, models []string) []relevantWindow {
	if u == nil {
		return nil
	}
	var out []relevantWindow
	if u.FiveHour != nil {
		out = append(out, relevantWindow{Label: "5h", Pct: u.FiveHour.Pct, ResetsAt: u.FiveHour.ResetsAt})
	}
	if u.SevenDay != nil {
		out = append(out, relevantWindow{Label: "7d", Pct: u.SevenDay.Pct, ResetsAt: u.SevenDay.ResetsAt})
	}
	if len(models) == 0 || len(u.Scoped) == 0 {
		return out
	}
	wanted := make(map[string]struct{}, len(models))
	matchAll := false
	for _, m := range models {
		lower := toLower(m)
		if lower == "all" {
			matchAll = true
		}
		wanted[lower] = struct{}{}
	}
	for _, s := range u.Scoped {
		if !matchAll {
			if _, ok := wanted[toLower(s.Name)]; !ok {
				continue
			}
		}
		out = append(out, relevantWindow{Label: s.Name, Pct: s.Pct, ResetsAt: s.ResetsAt})
	}
	return out
}

// toLower is a tiny, allocation-cheap ASCII+Unicode lowercase helper reused
// so this file does not need to import strings solely for EqualFold-style
// comparisons — kept local and trivial rather than pulled in as a dependency
// decision.
func toLower(s string) string {
	out := make([]rune, 0, len(s))
	for _, r := range s {
		if r >= 'A' && r <= 'Z' {
			r = r - 'A' + 'a'
		}
		out = append(out, r)
	}
	return string(out)
}

// AccountHeadroom is the remaining percentage before u hits a rate-limit
// window: 100 - max(relevant pcts). ok is false when usage is unavailable or
// carries no window data ("unknown" — callers must never auto-skip on this).
// Mirrors tsamx.oauth.account_headroom.
func AccountHeadroom(u *provider.Usage, models []string) (headroom float64, ok bool) {
	windows := RelevantWindows(u, models)
	if len(windows) == 0 {
		return 0, false
	}
	max := windows[0].Pct
	for _, w := range windows[1:] {
		if w.Pct > max {
			max = w.Pct
		}
	}
	return 100.0 - max, true
}

// BindingPct is the utilization of the binding (worst) relevant window.
// Mirrors tsamx.poll_policy.binding_pct.
func BindingPct(u *provider.Usage, models []string) (pct float64, ok bool) {
	headroom, ok := AccountHeadroom(u, models)
	if !ok {
		return 0, false
	}
	return 100.0 - headroom, true
}

// ParseResetTS parses an ISO-8601 resets_at string (as the usage API sends
// it, e.g. "2026-08-23T10:15:00Z" or with an explicit offset) into a
// time.Time. ok is false for an empty or unparseable string. Mirrors
// tsamx.poll_policy.parse_reset_ts (which parses via
// datetime.fromisoformat after replacing a trailing "Z"); Go's RFC3339
// parsing accepts "Z" natively so no such rewrite is needed here — this is a
// parser-implementation difference only, not a policy difference: both
// produce the same instant for the same well-formed input.
func ParseResetTS(resetsAt string) (time.Time, bool) {
	if resetsAt == "" {
		return time.Time{}, false
	}
	if t, err := time.Parse(time.RFC3339Nano, resetsAt); err == nil {
		return t, true
	}
	if t, err := time.Parse(time.RFC3339, resetsAt); err == nil {
		return t, true
	}
	return time.Time{}, false
}

// LimitingResetTS is the epoch when the LAST of the >=100%-utilized relevant
// windows resets (the point the account becomes usable again). Mirrors
// tsamx.poll_policy.limiting_reset_ts.
func LimitingResetTS(u *provider.Usage, models []string) (time.Time, bool) {
	var latest time.Time
	found := false
	for _, w := range RelevantWindows(u, models) {
		if w.Pct < 100.0 {
			continue
		}
		ts, ok := ParseResetTS(w.ResetsAt)
		if !ok {
			continue
		}
		if !found || ts.After(latest) {
			latest = ts
			found = true
		}
	}
	return latest, found
}

// EarliestFutureResetTS is the epoch of the next relevant-window reset ahead
// of now, at any utilization. Mirrors tsamx.poll_policy.earliest_future_reset_ts.
func EarliestFutureResetTS(u *provider.Usage, now time.Time, models []string) (time.Time, bool) {
	var earliest time.Time
	found := false
	for _, w := range RelevantWindows(u, models) {
		ts, ok := ParseResetTS(w.ResetsAt)
		if !ok || !ts.After(now) {
			continue
		}
		if !found || ts.Before(earliest) {
			earliest = ts
			found = true
		}
	}
	return earliest, found
}

// PlanInput is the input to PlanAfterFetch, mirroring plan_after_fetch's
// keyword arguments. PrevIntervalS is a pointer because "no previous
// interval" (Python's None) and "an explicit zero interval" both fall back
// to the state's default cadence (Python: `base = prev_interval_s or
// default`, which treats 0.0 the same as None) — a nil pointer OR a pointer
// to 0 both take that fallback here, matching the Python truthiness exactly.
type PlanInput struct {
	PrevIntervalS *float64
	PrevUsage     *provider.Usage
	NewUsage      *provider.Usage
	IsActive      bool
	ThresholdPct  float64
	Models        []string
	// Recent429 is true when a 429 was observed on this token within
	// RecentWindow429S of Now. This package is a pure function and keeps no
	// state of its own — the caller (a future P5 scheduler) is responsible
	// for persisting the last-429 timestamp per account and computing this,
	// e.g. via Recent429At.
	Recent429 bool
	Now       time.Time
	// Rand supplies the jitter random source in [0, 1). Injectable so tests
	// are deterministic (time.Now() and math/rand's global source must never
	// be called directly inside PlanAfterFetch). Defaults to math/rand's
	// global Float64 when nil, mirroring Python's `rng: Callable[[], float]
	// = random.random` default.
	Rand func() float64
}

// Plan is PlanAfterFetch's result, mirroring plan_after_fetch's
// (next_poll_at, interval_s) tuple.
type Plan struct {
	NextPollAt time.Time
	IntervalS  float64
}

// PlanAfterFetch computes the next poll time and interval for an account
// just fetched successfully. Ported branch-for-branch from
// tsamx.poll_policy.plan_after_fetch — see that function's docstring (and
// this package's doc comment) for the reasoning behind each branch; nothing
// here should diverge from it without also updating poll_policy.py (or
// documenting, in the port, exactly why Go has to differ).
//
// Movement (binding pct changed >= MovementDeltaPct since the previous poll)
// halves the interval, floored at MinIntervalS — or drops to UrgentIntervalS
// when the active account is moving inside the escalation band. No movement
// backs off x1.5 toward the account's ceiling; unknown utilization uses the
// default. A recent 429 on this token floors the cadence at
// Post429MinIntervalS (and, because it requires !Recent429, always
// suppresses urgent mode) until RecentWindow429S has passed. The scheduled
// time gets JitterFrac noise and is never later than the account's next
// window reset (+ ResetSlackS). An at-limit account keeps a bounded slow
// poll instead of sleeping until that reset, so an early provider-side quota
// grant is observed promptly.
func PlanAfterFetch(in PlanInput) Plan {
	rng := in.Rand
	if rng == nil {
		rng = rand.Float64
	}

	var defaultInterval, ceiling float64
	if in.IsActive {
		defaultInterval = MinIntervalS
		ceiling = ActiveMaxIntervalS
	} else {
		defaultInterval = CandidateDefaultIntervalS
		ceiling = CandidateMaxIntervalS
	}

	base := defaultInterval
	if in.PrevIntervalS != nil && *in.PrevIntervalS != 0 {
		base = *in.PrevIntervalS
	}

	prevPct, prevOk := BindingPct(in.PrevUsage, in.Models)
	newPct, newOk := BindingPct(in.NewUsage, in.Models)

	var moving bool
	var interval float64
	switch {
	case !prevOk || !newOk:
		moving = false
		interval = defaultInterval
	case math.Abs(newPct-prevPct) >= MovementDeltaPct:
		moving = true
		interval = math.Max(MinIntervalS, base/2)
	default:
		// Floored so a sub-floor base (urgent mode's 60s) snaps straight back
		// to the normal cadence once movement stops, instead of decaying
		// through 90s/135s polls the budget never intended (poll_policy.py's
		// comment on this exact branch).
		moving = false
		interval = math.Min(ceiling, math.Max(MinIntervalS, base*1.5))
	}

	if in.IsActive && moving && !in.Recent429 && newOk && newPct >= in.ThresholdPct-EscalationMarginPct {
		interval = UrgentIntervalS
	}

	if in.Recent429 {
		increased := math.Max(base*Post429BackoffMult, Post429MinIntervalS)
		interval = math.Min(Post429MaxIntervalS, math.Max(interval, increased))
	}

	headroom, headroomOk := AccountHeadroom(in.NewUsage, in.Models)
	exhausted := headroomOk && headroom <= 0
	if exhausted {
		interval = math.Max(interval, ExhaustedIntervalS)
	}

	jitter := JitterFrac * (2.0*rng() - 1.0)
	nextPoll := in.Now.Add(secondsToDuration(interval * (1.0 + jitter)))

	if exhausted {
		if resetTS, ok := LimitingResetTS(in.NewUsage, in.Models); ok && resetTS.After(in.Now) {
			if cand := resetTS.Add(secondsToDuration(ResetSlackS)); cand.Before(nextPoll) {
				nextPoll = cand
			}
		}
	} else {
		if resetTS, ok := EarliestFutureResetTS(in.NewUsage, in.Now, in.Models); ok {
			if cand := resetTS.Add(secondsToDuration(ResetSlackS)); cand.Before(nextPoll) {
				nextPoll = cand
			}
		}
	}

	return Plan{NextPollAt: nextPoll, IntervalS: interval}
}

// Recent429At reports whether last429At (the zero Time means "never seen")
// falls within RecentWindow429S of now — the convenience a caller uses to
// compute PlanInput.Recent429 from a persisted last-429 timestamp, without
// duplicating the window arithmetic at every call site.
func Recent429At(last429At, now time.Time) bool {
	if last429At.IsZero() {
		return false
	}
	return now.Sub(last429At) < secondsToDuration(RecentWindow429S)
}

// secondsToDuration converts a float64 seconds value (as every constant and
// intermediate interval in this file is expressed) to a time.Duration.
func secondsToDuration(s float64) time.Duration {
	return time.Duration(s * float64(time.Second))
}
