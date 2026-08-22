package usage

import (
	"testing"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
)

// t0 is a fixed instant every table case below anchors its expectations to,
// so tests never depend on time.Now().
var t0 = time.Date(2026, 8, 23, 0, 0, 0, 0, time.UTC)

// fiveHourUsage builds a *provider.Usage carrying only a five_hour window —
// exactly what RelevantWindows/BindingPct/AccountHeadroom need to reduce to
// "the pct" in every hand-computed case below (a lone window is trivially
// its own max).
func fiveHourUsage(pct float64, resetsAt string) *provider.Usage {
	return &provider.Usage{FiveHour: &provider.Window{Id: "five_hour", Pct: pct, ResetsAt: resetsAt, WindowMinutes: fiveHourWindowMinutes}}
}

func fixedRand(v float64) func() float64 { return func() float64 { return v } }

func floatPtr(v float64) *float64 { return &v }

// TestPlanAfterFetch is a table of representative branches, each hand-computed
// from tsamx/src/tsamx/poll_policy.py's plan_after_fetch by walking the exact
// same arithmetic the Python does (see poll_policy.py, read at the time this
// port was written: base = prev_interval_s or default; movement halves
// base/floors at MIN_INTERVAL_S; no-movement backs off *1.5 toward the
// state's ceiling; urgent mode requires is_active && moving && !recent_429 &&
// new_pct >= threshold - ESCALATION_MARGIN_PCT; a recent 429 grows the
// interval via increased = max(base*POST_429_BACKOFF_MULT,
// POST_429_MIN_INTERVAL_S) and interval = min(POST_429_MAX_INTERVAL_S,
// max(interval, increased)); headroom<=0 floors interval at
// EXHAUSTED_INTERVAL_S; jitter is +-JITTER_FRAC around interval; and the
// final next_poll is clamped to the relevant reset (+RESET_SLACK_S) — the
// LIMITING reset while exhausted, the EARLIEST future reset otherwise).
func TestPlanAfterFetch(t *testing.T) {
	cases := []struct {
		name         string
		in           PlanInput
		wantInterval float64
		wantNextPoll time.Time
	}{
		{
			// delta = |52-50| = 2 >= MOVEMENT_DELTA_PCT(1) -> moving.
			// interval = max(MIN_INTERVAL_S=180, base/2=300/2=150) = 180.
			// not active -> urgent skipped. not recent429 -> AIMD skipped.
			// headroom = 100-52 = 48 > 0 -> not exhausted.
			// rand=0.5 -> jitter term = 0.1*(1-1) = 0 -> next_poll = t0+180s.
			name: "movement halves interval and floors at MinIntervalS",
			in: PlanInput{
				PrevIntervalS: floatPtr(300),
				PrevUsage:     fiveHourUsage(50, ""),
				NewUsage:      fiveHourUsage(52, ""),
				IsActive:      false,
				ThresholdPct:  95,
				Now:           t0,
				Rand:          fixedRand(0.5),
			},
			wantInterval: 180,
			wantNextPoll: t0.Add(180 * time.Second),
		},
		{
			// delta = 0.3 < 1 -> not moving.
			// interval = min(CandidateMaxIntervalS=600, max(180, 300*1.5=450)) = 450.
			// headroom = 100-50.3 = 49.7 > 0.
			name: "no movement backs off toward candidate ceiling",
			in: PlanInput{
				PrevIntervalS: floatPtr(300),
				PrevUsage:     fiveHourUsage(50, ""),
				NewUsage:      fiveHourUsage(50.3, ""),
				IsActive:      false,
				ThresholdPct:  95,
				Now:           t0,
				Rand:          fixedRand(0.5),
			},
			wantInterval: 450,
			wantNextPoll: t0.Add(450 * time.Second),
		},
		{
			// PrevUsage nil -> prevPct unknown -> moving=false, interval=default.
			// is_active=false -> default = CandidateDefaultIntervalS = 300.
			name: "unknown previous utilization uses the state default",
			in: PlanInput{
				PrevUsage:    nil,
				NewUsage:     fiveHourUsage(10, ""),
				IsActive:     false,
				ThresholdPct: 95,
				Now:          t0,
				Rand:         fixedRand(0.5),
			},
			wantInterval: 300,
			wantNextPoll: t0.Add(300 * time.Second),
		},
		{
			// delta = 3 >= 1 -> moving; interval = max(180, 180/2=90) = 180.
			// urgent: active && moving && !recent429 && 81 >= 95-15=80 -> true
			// -> interval = UrgentIntervalS = 60.
			name: "active account moving inside escalation band goes urgent",
			in: PlanInput{
				PrevIntervalS: floatPtr(180),
				PrevUsage:     fiveHourUsage(78, ""),
				NewUsage:      fiveHourUsage(81, ""),
				IsActive:      true,
				ThresholdPct:  95,
				Now:           t0,
				Rand:          fixedRand(0.5),
			},
			wantInterval: 60,
			wantNextPoll: t0.Add(60 * time.Second),
		},
		{
			// delta = 0.2 < 1 -> not moving.
			// interval = min(600, max(180, 200*1.5=300)) = 300.
			// recent429: increased = max(200*1.5=300, POST_429_MIN=360) = 360.
			// interval = min(1800, max(300, 360)) = 360 -> the 360s floor wins.
			name: "recent 429 floors interval at Post429MinIntervalS",
			in: PlanInput{
				PrevIntervalS: floatPtr(200),
				PrevUsage:     fiveHourUsage(50, ""),
				NewUsage:      fiveHourUsage(50.2, ""),
				IsActive:      false,
				ThresholdPct:  95,
				Recent429:     true,
				Now:           t0,
				Rand:          fixedRand(0.5),
			},
			wantInterval: 360,
			wantNextPoll: t0.Add(360 * time.Second),
		},
		{
			// delta = 0.4 < 1 -> not moving.
			// interval = min(600, max(180, 500*1.5=750)) = 600 (ceiling clip).
			// recent429: increased = max(500*1.5=750, 360) = 750.
			// interval = min(1800, max(600, 750)) = 750 -> AIMD grows PAST the
			// non-429 candidate ceiling, matching the "wider than the normal
			// candidate ceiling" comment in poll_policy.py.
			name: "recent 429 AIMD grows past the non-429 ceiling",
			in: PlanInput{
				PrevIntervalS: floatPtr(500),
				PrevUsage:     fiveHourUsage(50, ""),
				NewUsage:      fiveHourUsage(50.4, ""),
				IsActive:      false,
				ThresholdPct:  95,
				Recent429:     true,
				Now:           t0,
				Rand:          fixedRand(0.5),
			},
			wantInterval: 750,
			wantNextPoll: t0.Add(750 * time.Second),
		},
		{
			// delta = 10 >= 1 -> moving; interval = max(180, 90) = 180.
			// headroom = 100-100 = 0 <= 0 -> exhausted -> interval =
			// max(180, ExhaustedIntervalS=600) = 600.
			// baseline next_poll = t0+600s, but the exhausted branch clamps to
			// the LIMITING reset (the >=100% window's resets_at, t0+120s) plus
			// RESET_SLACK_S=60 -> t0+180s, which is earlier than t0+600s.
			name: "exhausted account keeps a bounded slow poll clamped to its reset",
			in: PlanInput{
				PrevIntervalS: floatPtr(180),
				PrevUsage:     fiveHourUsage(90, ""),
				NewUsage:      fiveHourUsage(100, t0.Add(120*time.Second).Format(time.RFC3339)),
				IsActive:      false,
				ThresholdPct:  95,
				Now:           t0,
				Rand:          fixedRand(0.5),
			},
			wantInterval: 600,
			wantNextPoll: t0.Add(180 * time.Second),
		},
		{
			// Not exhausted (headroom 90>0), default interval=300 (both usages
			// unknown pct is not the case here: NewUsage known, PrevUsage nil ->
			// still falls into the "unknown" branch since prevOk is false).
			// The upcoming reset (t0+50s) is sooner than the 300s baseline, so
			// next_poll clamps to reset+RESET_SLACK_S = t0+110s. interval itself
			// is untouched by this clamp (matches poll_policy.py returning
			// (next_poll, interval) as independently computed values).
			name: "imminent reset clamps next_poll without changing interval",
			in: PlanInput{
				PrevUsage:    nil,
				NewUsage:     fiveHourUsage(10, t0.Add(50*time.Second).Format(time.RFC3339)),
				IsActive:     false,
				ThresholdPct: 95,
				Now:          t0,
				Rand:         fixedRand(0.5),
			},
			wantInterval: 300,
			wantNextPoll: t0.Add(110 * time.Second),
		},
		{
			// Both usages nil/unknown -> default interval = 300 (candidate).
			// jitter lower bound: rand()=0 -> jitter = 0.1*(2*0-1) = -0.1 ->
			// factor 0.9 -> next_poll = t0 + 300*0.9 = t0+270s.
			name: "jitter lower bound at rand()=0",
			in: PlanInput{
				IsActive:     false,
				ThresholdPct: 95,
				Now:          t0,
				Rand:         fixedRand(0),
			},
			wantInterval: 300,
			wantNextPoll: t0.Add(270 * time.Second),
		},
		{
			// Same as above but rand()=1 -> jitter = 0.1*(2*1-1) = 0.1 ->
			// factor 1.1 -> next_poll = t0 + 300*1.1 = t0+330s.
			name: "jitter upper bound at rand()=1",
			in: PlanInput{
				IsActive:     false,
				ThresholdPct: 95,
				Now:          t0,
				Rand:         fixedRand(1),
			},
			wantInterval: 300,
			wantNextPoll: t0.Add(330 * time.Second),
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := PlanAfterFetch(tc.in)
			if got.IntervalS != tc.wantInterval {
				t.Errorf("IntervalS = %v, want %v", got.IntervalS, tc.wantInterval)
			}
			if !got.NextPollAt.Equal(tc.wantNextPoll) {
				t.Errorf("NextPollAt = %v, want %v", got.NextPollAt, tc.wantNextPoll)
			}
		})
	}
}

// TestPlanAfterFetch_NilRandDoesNotPanic exercises the default-rand fallback
// path (in.Rand == nil) — it must produce a plan without calling time.Now()
// or panicking, though the exact next_poll depends on the global math/rand
// source and is not asserted here.
func TestPlanAfterFetch_NilRandDoesNotPanic(t *testing.T) {
	got := PlanAfterFetch(PlanInput{IsActive: false, ThresholdPct: 95, Now: t0})
	if got.IntervalS != 300 {
		t.Fatalf("IntervalS = %v, want 300 (candidate default)", got.IntervalS)
	}
	if got.NextPollAt.Before(t0.Add(270*time.Second)) || got.NextPollAt.After(t0.Add(330*time.Second)) {
		t.Fatalf("NextPollAt = %v, want within [t0+270s, t0+330s]", got.NextPollAt)
	}
}

// TestPlanAfterFetch_ZeroPrevIntervalFallsBackToDefault pins Python's
// `base = prev_interval_s or default` truthiness: a pointer to 0.0 must be
// treated exactly like a nil pointer (both fall back to the state default),
// not like "an explicit, tiny previous interval".
func TestPlanAfterFetch_ZeroPrevIntervalFallsBackToDefault(t *testing.T) {
	withZero := PlanAfterFetch(PlanInput{
		PrevIntervalS: floatPtr(0),
		IsActive:      false,
		ThresholdPct:  95,
		Now:           t0,
		Rand:          fixedRand(0.5),
	})
	withNil := PlanAfterFetch(PlanInput{
		PrevIntervalS: nil,
		IsActive:      false,
		ThresholdPct:  95,
		Now:           t0,
		Rand:          fixedRand(0.5),
	})
	if withZero != withNil {
		t.Fatalf("zero prev interval = %+v, nil prev interval = %+v, want equal", withZero, withNil)
	}
}

func TestParseResetTS(t *testing.T) {
	cases := []struct {
		name  string
		input string
		ok    bool
	}{
		{"empty", "", false},
		{"garbage", "not-a-time", false},
		{"zulu", "2026-08-23T10:15:00Z", true},
		{"offset", "2026-08-23T19:15:00+09:00", true},
		{"fractional zulu", "2026-08-23T10:15:00.123456Z", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, ok := ParseResetTS(tc.input)
			if ok != tc.ok {
				t.Fatalf("ParseResetTS(%q) ok = %v, want %v", tc.input, ok, tc.ok)
			}
		})
	}
	// Zulu and an equivalent explicit offset must parse to the same instant.
	zulu, _ := ParseResetTS("2026-08-23T10:15:00Z")
	offset, _ := ParseResetTS("2026-08-23T19:15:00+09:00")
	if !zulu.Equal(offset) {
		t.Fatalf("Zulu and equivalent offset parsed to different instants: %v vs %v", zulu, offset)
	}
}

func TestRelevantWindows_ScopedModelFilter(t *testing.T) {
	u := &provider.Usage{
		FiveHour: &provider.Window{Pct: 10},
		SevenDay: &provider.Window{Pct: 20},
		Scoped: []provider.ScopedWindow{
			{Name: "Fable", Pct: 30},
			{Name: "Sonnet", Pct: 40},
		},
	}

	// No models requested -> only the always-present 5h/7d windows.
	if got := RelevantWindows(u, nil); len(got) != 2 {
		t.Fatalf("no models: got %d windows, want 2", len(got))
	}

	// Named model, matched case-insensitively.
	got := RelevantWindows(u, []string{"fable"})
	if len(got) != 3 {
		t.Fatalf("named model: got %d windows, want 3 (5h+7d+Fable)", len(got))
	}
	foundFable := false
	for _, w := range got {
		if w.Label == "Fable" && w.Pct == 30 {
			foundFable = true
		}
	}
	if !foundFable {
		t.Fatalf("named model: Fable scoped window missing from %+v", got)
	}

	// "all" sentinel matches every scoped window.
	got = RelevantWindows(u, []string{"all"})
	if len(got) != 4 {
		t.Fatalf("all sentinel: got %d windows, want 4 (5h+7d+Fable+Sonnet)", len(got))
	}
}

func TestAccountHeadroom_UnknownWhenNoWindows(t *testing.T) {
	if _, ok := AccountHeadroom(nil, nil); ok {
		t.Fatal("AccountHeadroom(nil, nil) ok = true, want false (unknown)")
	}
	if _, ok := AccountHeadroom(&provider.Usage{}, nil); ok {
		t.Fatal("AccountHeadroom(empty Usage, nil) ok = true, want false (unknown)")
	}
}

// TestRecent429_NoLiveBackoff exercises Recent429 with no backoff state at
// all (backoffUntil zero, lastError "") — the anchor stays last429At itself,
// same as the naive (and, per C1/C3 review, WRONG for the live-backoff case
// below) version this function replaced.
func TestRecent429_NoLiveBackoff(t *testing.T) {
	if Recent429(time.Time{}, time.Time{}, "", t0) {
		t.Fatal("zero last429At must never count as recent")
	}
	if !Recent429(t0, time.Time{}, "", t0.Add(1*time.Hour-time.Second)) {
		t.Fatal("429 59m59s ago (no live backoff) must still count as recent (RecentWindow429S=3600)")
	}
	if Recent429(t0, time.Time{}, "", t0.Add(1*time.Hour+time.Second)) {
		t.Fatal("429 1h0m1s ago (no live backoff) must no longer count as recent")
	}
}

// TestRecent429_AnchorsOnBackoffEnd_NotOnTheBlockStart is the C3 fix's whole
// point: an hour-scale Retry-After is honored as ONE long backoff during
// which no attempt runs, so the 429 leaves only one stamp — at the block's
// START (last429At). A naive `now - last429At < RecentWindow429S` check
// already reads "not recent" at the very instant the first post-block
// success happens (elapsed == the block length), so AIMD would never
// engage. Anchoring on backoffUntil (the block's END) instead keeps it
// "recent" across exactly that first post-block success.
func TestRecent429_AnchorsOnBackoffEnd_NotOnTheBlockStart(t *testing.T) {
	last429At := t0
	backoffUntil := t0.Add(1 * time.Hour) // an hour-scale Retry-After, honored in full
	now := backoffUntil                   // the first attempt after the block lifts

	if Recent429(last429At, backoffUntil, "http-429", now) == false {
		t.Fatal("Recent429 = false at the block's own end, want true (this is the bug C3 fixes)")
	}
	// The naive anchor (last429At only) would already read false here:
	if now.Sub(last429At) < secondsToDuration(RecentWindow429S) {
		t.Fatal("test setup invalid: now-last429At should already exceed RecentWindow429S at the block's end")
	}
	// Bound is RecentWindow429S PAST THE ANCHOR (backoffUntil), not past
	// last429At: still recent just before backoffUntil+3600s...
	if !Recent429(last429At, backoffUntil, "http-429", backoffUntil.Add(secondsToDuration(RecentWindow429S)-time.Second)) {
		t.Fatal("Recent429 = false just before backoffUntil+RecentWindow429S, want true")
	}
	// ...and no longer recent just after it.
	if Recent429(last429At, backoffUntil, "http-429", backoffUntil.Add(secondsToDuration(RecentWindow429S)+time.Second)) {
		t.Fatal("Recent429 = true just after backoffUntil+RecentWindow429S, want false")
	}
}

// TestRecent429_LastErrorGuardIgnoresUnrelatedBackoff pins the lastError
// guard: backoffUntil/lastError are overwritten by ANY later failure, but
// last429At is never cleared — so an unrelated timeout's backoffUntil must
// NOT be adopted as the 429 anchor, or an old, long-expired 429 would
// spuriously re-arm AIMD off a completely different failure.
func TestRecent429_LastErrorGuardIgnoresUnrelatedBackoff(t *testing.T) {
	last429At := t0 // a 429 a very long time ago
	// A later, unrelated timeout's backoff, well past the 429's own window —
	// if adopted as the anchor this would wrongly read "recent".
	unrelatedBackoffUntil := t0.Add(10 * time.Hour)
	now := t0.Add(10*time.Hour + time.Minute)

	if Recent429(last429At, unrelatedBackoffUntil, "timeout", now) {
		t.Fatal("Recent429 adopted an unrelated (non-http-429) backoffUntil as its anchor")
	}
	// Sanity: the same backoffUntil, with lastError correctly "http-429",
	// DOES anchor there (proves the guard, not just a broken backoffUntil
	// plumbing, is what suppressed the case above).
	if !Recent429(last429At, unrelatedBackoffUntil, "http-429", now) {
		t.Fatal("Recent429 should anchor on backoffUntil when lastError is http-429")
	}
}

// TestRecent429_BackoffUntilNotAfterAnchorIsIgnored covers the
// `backoffUntil.After(anchor)` half of the guard: a backoffUntil at or
// before last429At (stale/zero) must not move the anchor backward.
func TestRecent429_BackoffUntilNotAfterAnchorIsIgnored(t *testing.T) {
	last429At := t0
	staleBackoffUntil := t0.Add(-1 * time.Minute) // before last429At; must be ignored
	now := t0.Add(1*time.Hour - time.Second)
	if !Recent429(last429At, staleBackoffUntil, "http-429", now) {
		t.Fatal("Recent429 should still anchor on last429At when backoffUntil is not after it")
	}
}

// TestFailureBackoffS is a table hand-computed from
// tsamx.usage_store._failure_backoff_s (M1, P4 review): the next-poll
// calculator this package's original P4 draft was missing entirely.
//
//	computed = min(BackoffBaseS * 2^min(max(0,failures-1),BackoffMaxShift), BackoffCapS)
//	no Retry-After                          -> computed
//	Retry-After == 0, rateLimited           -> min(max(computed,EdgeBackoffS),BackoffCapS)
//	Retry-After == 0, !rateLimited          -> computed
//	Retry-After > 0:
//	  asked = retryAfterS
//	  if retryAfterS > BackoffCapS && rateLimited: asked += RetryAfterMarginS
//	  asked = min(asked, rateLimited ? RetryAfterFloorCapS : TrustMaxAgeS)
//	  return max(asked, computed)
func TestFailureBackoffS(t *testing.T) {
	cases := []struct {
		name         string
		failures     int
		retryAfterS  *float64
		rateLimited  bool
		wantBackoffS float64
	}{
		{
			// shift = min(max(0,0),32) = 0 -> 30*2^0 = 30.
			name:     "no retry-after, first failure",
			failures: 1, retryAfterS: nil, rateLimited: true,
			wantBackoffS: 30,
		},
		{
			// shift = 3 -> 30*8 = 240.
			name:     "no retry-after, exponential growth",
			failures: 4, retryAfterS: nil, rateLimited: true,
			wantBackoffS: 240,
		},
		{
			// shift = 9 -> 30*512 = 15360, capped at 600.
			name:     "no retry-after, saturates at BackoffCapS",
			failures: 10, retryAfterS: nil, rateLimited: true,
			wantBackoffS: 600,
		},
		{
			// computed(failures=1)=30; rate-limited Retry-After:0 floors at
			// EdgeBackoffS(300): min(max(30,300),600) = 300.
			name:     "retry-after 0, rate-limited (saturated edge) floors at EdgeBackoffS",
			failures: 1, retryAfterS: floatPtr(0), rateLimited: true,
			wantBackoffS: 300,
		},
		{
			// Non-429 Retry-After:0 (e.g. a Cloudflare 503) falls through to
			// the plain curve: computed(failures=1)=30.
			name:     "retry-after 0, NOT rate-limited falls through to the plain curve",
			failures: 1, retryAfterS: floatPtr(0), rateLimited: false,
			wantBackoffS: 30,
		},
		{
			// asked=120 (<=BackoffCapS, no margin); min(120,4500)=120;
			// max(120, computed=30) = 120.
			name:     "small rate-limited retry-after under BackoffCapS: no margin added",
			failures: 1, retryAfterS: floatPtr(120), rateLimited: true,
			wantBackoffS: 120,
		},
		{
			// asked=3600+900(margin, since 3600>BackoffCapS)=4500;
			// min(4500,RetryAfterFloorCapS=4500)=4500; max(4500,30)=4500.
			name:     "large rate-limited retry-after gets the margin then floor-capped",
			failures: 1, retryAfterS: floatPtr(3600), rateLimited: true,
			wantBackoffS: 4500,
		},
		{
			// asked=10000+900=10900 -> min(10900,4500)=4500 (still capped,
			// regardless of how far past the cap the ask was).
			name:     "absurd rate-limited retry-after still bounded at RetryAfterFloorCapS",
			failures: 1, retryAfterS: floatPtr(10000), rateLimited: true,
			wantBackoffS: 4500,
		},
		{
			// NOT rate-limited: asked=3600 (no margin arm at all — that arm
			// requires rateLimited); min(3600, TrustMaxAgeS=3600)=3600;
			// max(3600,30)=3600 — bounded by the SMALLER, different ceiling.
			name:     "non-rate-limited retry-after bounded by TrustMaxAgeS not RetryAfterFloorCapS",
			failures: 1, retryAfterS: floatPtr(5000), rateLimited: false,
			wantBackoffS: 3600,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := FailureBackoffS(tc.failures, tc.retryAfterS, tc.rateLimited)
			if got != tc.wantBackoffS {
				t.Fatalf("FailureBackoffS(%d, %v, %v) = %v, want %v", tc.failures, tc.retryAfterS, tc.rateLimited, got, tc.wantBackoffS)
			}
		})
	}
}

func TestNextPollAfterFetchError(t *testing.T) {
	if got := NextPollAfterFetchError(t0, 0, nil); !got.Equal(t0) {
		t.Fatalf("nil FetchError: next poll = %v, want unchanged t0 = %v", got, t0)
	}

	// A first 429 (failuresBefore=0 -> failures=1) with no Retry-After:
	// computed(1)=30, rate-limited edge doesn't apply (retryAfterS is nil,
	// not 0) -> backoff=30s.
	got := NextPollAfterFetchError(t0, 0, &FetchError{Kind: "http-429"})
	if want := t0.Add(30 * time.Second); !got.Equal(want) {
		t.Fatalf("429 no retry-after: next poll = %v, want %v", got, want)
	}

	// A second consecutive 429 (failuresBefore=1 -> failures=2) with
	// Retry-After: 90 (rate-limited, under BackoffCapS -> no margin):
	// asked=90, computed(2)=min(30*2,600)=60, max(90,60)=90.
	retryAfter := 90.0
	got = NextPollAfterFetchError(t0, 1, &FetchError{Kind: "http-429", RetryAfterS: &retryAfter})
	if want := t0.Add(90 * time.Second); !got.Equal(want) {
		t.Fatalf("429 with retry-after 90: next poll = %v, want %v", got, want)
	}

	// A non-429 failure (rateLimited=false) with no Retry-After:
	// computed(1)=30.
	got = NextPollAfterFetchError(t0, 0, &FetchError{Kind: "timeout"})
	if want := t0.Add(30 * time.Second); !got.Equal(want) {
		t.Fatalf("timeout: next poll = %v, want %v", got, want)
	}
}
