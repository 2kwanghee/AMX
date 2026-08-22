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
//
// KNOWN RISK, NOT CLOSED BY THIS PACKAGE (P6 shadow-run double consumption):
// tsamx's own collector persists its plan (nextPollAt/pollIntervalS) and its
// 429/backoff history in its usage store (cache/usage.json — see
// tsamx/src/tsamx/usage_store.py's module docstring), and every tsamx-side
// surface (list/status/TUI/menu bar/auto) reads and contributes to that ONE
// lease so they collectively stay under the measured budget. This package
// has no access to that store and cannot join its lease. If a future P6
// shadow run ever calls this package's Collector.Fetch on a cadence of its
// own WHILE tsamx also polls the same identity, the two are two independent
// consumers of one shared, machine-invisible budget (see the docstring
// above) — their combined rate is not bounded by either side's policy alone,
// and a 429 either one draws can floor the OTHER's cadence too (the 429 is
// scoped to the account/token, not to which process asked). Two options for
// whoever wires P6, neither implemented here:
//
//	(a) shadow-only comparison: P6 reads tsamx's persisted cache/usage.json
//	    measurement to compare against, and this package's Collector never
//	    calls the network itself in shadow mode;
//	(b) real lease-sharing: this package's future scheduler joins tsamx's
//	    usage_store.py lease/claim protocol (or a successor store both
//	    engines share) instead of planning independently.
//
// This is a documentation-only note per the P4 review (M4); no code in this
// package guards against double consumption, and none should be added here
// without first deciding (a) vs (b) at the design-note level.
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
	Post429BackoffMult  = 1.5
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

// Failure-backoff constants, ported from tsamx/src/tsamx/usage_store.py
// (NOT poll_policy.py — this is the store's per-failure "when to try again"
// curve, a different concern from plan_after_fetch's per-success cadence
// above, but the P4 review (M1) asked for it explicitly: "실패 종류별 다음
// 시각 계산" — the thing that actually decides the next poll after a 429,
// timeout, or other fetch error, which nothing in this package computed
// before this port).
const (
	// BackoffBaseS/BackoffCapS/BackoffMaxShift: the plain exponential curve
	// used when the server sent no Retry-After (or a non-rate-limited
	// Retry-After of 0): BackoffBaseS * 2^min(max(0,failures-1),
	// BackoffMaxShift), capped at BackoffCapS.
	BackoffBaseS    = 30.0
	BackoffCapS     = 600.0
	BackoffMaxShift = 32

	// RetryAfterMarginS is added to a rate-limited (429) Retry-After ask
	// ABOVE BackoffCapS, before the RetryAfterFloorCapS clamp below — see
	// usage_store.py's RETRY_AFTER_MARGIN_S comment (measured: 20 of 35
	// lapsed blocks re-blocked within 900s of their own stated deadline).
	RetryAfterMarginS = 900.0

	// RetryAfterFloorCapS bounds a rate-limited (429) ask, margin included,
	// so a pathological or absurd Retry-After (an "Infinity" literal, a
	// multi-day value) cannot park an account indefinitely. A non-rate-
	// limited ask is bounded by TrustMaxAgeS instead (see FailureBackoffS).
	RetryAfterFloorCapS = 4500.0

	// TrustMaxAgeS bounds a NON-rate-limited failure's Retry-After ask (a
	// 503/504 etc. can also carry the header); it is a different, smaller
	// ceiling than RetryAfterFloorCapS because a non-429 last_good value
	// stops being decision-trusted after this many seconds regardless
	// (usage_store.py's TRUST_MAX_AGE_S), so parking the row longer than
	// that would leave it both un-pollable and unknown at the same time.
	TrustMaxAgeS = 3600.0
)

// FailureBackoffS is the seconds to wait before the next attempt after a
// failed fetch, ported branch-for-branch from
// tsamx.usage_store._failure_backoff_s. consecutiveFailures is the failure
// streak INCLUDING this one (matches the Python call site: `failures =
// consecutive_failures + 1` is computed by the caller before this is
// called — see NextPollAfterFetchError). retryAfterS is the server's
// Retry-After in seconds when present (nil when absent, matching Python's
// `retry_after_s: float | None`). rateLimited must be true only for a 429
// (Python: `rate_limited=rec.error == "http-429"` at the one call site,
// usage_store.py's record()) — it selects which of the two ceilings
// (RetryAfterFloorCapS vs TrustMaxAgeS) bounds a large Retry-After ask, and
// whether a `Retry-After: 0` gets the EdgeBackoffS floor (the saturated-
// budget edge) or falls through to the plain exponential curve.
func FailureBackoffS(consecutiveFailures int, retryAfterS *float64, rateLimited bool) float64 {
	shift := consecutiveFailures - 1
	if shift < 0 {
		shift = 0
	}
	if shift > BackoffMaxShift {
		shift = BackoffMaxShift
	}
	computed := math.Min(BackoffBaseS*math.Pow(2, float64(shift)), BackoffCapS)

	if retryAfterS == nil {
		return computed
	}
	if *retryAfterS == 0 {
		if !rateLimited {
			// A `Retry-After: 0` on a non-429 (e.g. a Cloudflare 503 saying
			// "retry now") is not the saturated-budget edge poll_policy's
			// EdgeBackoffS was measured on; fall through to the plain curve.
			return computed
		}
		return math.Min(math.Max(computed, EdgeBackoffS), BackoffCapS)
	}

	asked := *retryAfterS
	if *retryAfterS > BackoffCapS && rateLimited {
		asked = *retryAfterS + RetryAfterMarginS
	}
	if rateLimited {
		asked = math.Min(asked, RetryAfterFloorCapS)
	} else {
		asked = math.Min(asked, TrustMaxAgeS)
	}
	return math.Max(asked, computed)
}

// NextPollAfterFetchError is the next-poll-time-by-failure-kind calculator
// the P4 review asked for: it turns a *FetchError from Collector.Fetch,
// together with the account's failure streak so far (NOT including this
// failure — this function adds 1 itself, matching usage_store.py record()'s
// `failures = int(row.get("consecutiveFailures") or 0) + 1` immediately
// before its own call to `_failure_backoff_s`), into the instant the next
// attempt should run. rateLimited is derived from fe.Kind == "http-429",
// matching the one call site in tsamx (usage_store.py's record:
// `rate_limited=rec.error == "http-429"`). A nil fe returns now unchanged
// (nothing to back off from).
//
// This package is stateless: the caller (a future P5 scheduler) owns
// persisting consecutiveFailuresBefore across calls, exactly as
// PlanAfterFetch's caller owns persisting prevIntervalS/last429At.
func NextPollAfterFetchError(now time.Time, consecutiveFailuresBefore int, fe *FetchError) time.Time {
	if fe == nil {
		return now
	}
	rateLimited := fe.Kind == "http-429"
	backoff := FailureBackoffS(consecutiveFailuresBefore+1, fe.RetryAfterS, rateLimited)
	return now.Add(secondsToDuration(backoff))
}

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

// Recent429 reports whether a token 429'd recently enough to keep
// PlanAfterFetch's post-429 cadence engaged, ported EXACTLY from
// usage_store.UsageEntry.recent_429 (usage_store.py:300-333) — a naive
// `now - last429At < RecentWindow429S` (this package's original, incorrect
// P4 draft) anchors on the WRONG instant and, per that method's own
// docstring, is precisely the failure mode it exists to avoid:
//
// An hour-scale Retry-After is honored as one long backoff during which no
// attempt runs, so the 429 leaves only ONE stamp, at the block's START
// (last429At). Anchoring recency on that start means the first success
// AFTER the block already lands at (or past) last429At + the block's own
// length — if that length is close to RecentWindow429S, the naive check
// already reads "not recent" at the very moment AIMD needs to be armed, so
// it never engages and machines sharing the token never converge.
//
// The fix anchors on the backoff's END instead — but ONLY while the LIVE
// backoff is actually a 429 backoff: last429At is (by this package's
// contract, mirroring the Python row) never cleared, while backoffUntil and
// lastError are overwritten by ANY later failure. Without the lastError
// guard, an unrelated timeout on a token that 429'd long ago would install a
// fresh backoffUntil and spuriously re-arm the post-429 cadence. A success
// clears lastError/backoffUntil entirely (mirrored here as an empty
// lastError / zero backoffUntil), so only a 429 can ever set both together.
//
//   - last429At: the zero Time means "never seen" -> never recent.
//   - backoffUntil/lastError: the CURRENT fetch-state fields the caller
//     already tracks alongside last429At (usage_store.py's UsageEntry rows
//     these exact three fields together) — pass the zero Time / "" when the
//     account has no live backoff.
//   - now: the instant recency is evaluated at.
func Recent429(last429At, backoffUntil time.Time, lastError string, now time.Time) bool {
	if last429At.IsZero() {
		return false
	}
	anchor := last429At
	if lastError == "http-429" && !backoffUntil.IsZero() && backoffUntil.After(anchor) {
		anchor = backoffUntil
	}
	return now.Before(anchor.Add(secondsToDuration(RecentWindow429S)))
}

// secondsToDuration converts a float64 seconds value (as every constant and
// intermediate interval in this file is expressed) to a time.Duration.
func secondsToDuration(s float64) time.Duration {
	return time.Duration(s * float64(time.Second))
}
