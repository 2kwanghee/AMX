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

func TestRecent429At(t *testing.T) {
	if Recent429At(time.Time{}, t0) {
		t.Fatal("zero last429At must never count as recent")
	}
	if !Recent429At(t0, t0.Add(1*time.Hour-time.Second)) {
		t.Fatal("429 59m59s ago must still count as recent (RecentWindow429S=3600)")
	}
	if Recent429At(t0, t0.Add(1*time.Hour+time.Second)) {
		t.Fatal("429 1h0m1s ago must no longer count as recent")
	}
}
