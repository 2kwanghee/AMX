package usage

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
)

func testRef(email string) AccountRef {
	return AccountRef{ProviderKey: "claude", AccountKey: testAccountKey(email)}
}

// testAccountKey mirrors profile.AccountKey's shape (64 lowercase hex chars)
// without importing the profile package into every test — a store.go test
// only needs A key shaped like a real one, not the actual hash rule.
func testAccountKey(seed string) string {
	const hexdigits = "0123456789abcdef"
	out := make([]byte, 64)
	for i := range out {
		out[i] = hexdigits[(int(seed[i%len(seed)])+i)%16]
	}
	return string(out)
}

func mustOpenStore(t *testing.T) *Store {
	t.Helper()
	s, err := OpenStore(t.TempDir())
	if err != nil {
		t.Fatalf("OpenStore: %v", err)
	}
	return s
}

func TestOpenStore_LayoutIsSeparateFromProfileAndDeliverLocks(t *testing.T) {
	base := t.TempDir()
	s, err := OpenStore(base)
	if err != nil {
		t.Fatalf("OpenStore: %v", err)
	}
	wantDir := filepath.Join(base, "usage")
	if info, err := os.Stat(wantDir); err != nil || !info.IsDir() {
		t.Fatalf("expected %s to be a directory: %v", wantDir, err)
	}
	if s.lockPath != filepath.Join(wantDir, ".usage.lock") {
		t.Errorf("lockPath = %q", s.lockPath)
	}
	// Must not collide with profile.Store's own subtree ("profiles") or any
	// file directly under stateDir (the deliver lock lives inside a config
	// home, not stateDir, but this at least proves usage/ owns its own tree).
	if filepath.Dir(s.lockPath) == base {
		t.Error("lock file must live under its own subdirectory, not stateDir directly")
	}
}

func TestClaim_StampsLeaseAndCreatesRow(t *testing.T) {
	s := mustOpenStore(t)
	ref := testRef("a@example.com")
	fixed := time.Date(2026, 8, 23, 0, 0, 0, 0, time.UTC)
	s.clock = func() time.Time { return fixed }

	claims, err := s.Claim([]AccountRef{ref})
	if err != nil {
		t.Fatalf("Claim: %v", err)
	}
	id, ok := claims[ref]
	if !ok || id == "" {
		t.Fatalf("claims[ref] = %q, %v, want a non-empty id", id, ok)
	}

	entries, recovered, err := s.Entries([]AccountRef{ref})
	if err != nil || recovered {
		t.Fatalf("Entries: recovered=%v err=%v", recovered, err)
	}
	e := entries[ref]
	if e.LastAttemptAt.IsZero() || !e.LastAttemptAt.Equal(fixed) {
		t.Errorf("LastAttemptAt = %v, want %v", e.LastAttemptAt, fixed)
	}
	if e.ClaimUntil == nil {
		t.Fatal("ClaimUntil = nil, want a stamped lease")
	}
	wantUntil := fixed.Add(secondsToDuration(ClaimTTLS))
	if !e.ClaimUntil.Equal(wantUntil) {
		t.Errorf("ClaimUntil = %v, want %v", *e.ClaimUntil, wantUntil)
	}
	if !e.Claimed(fixed) {
		t.Error("Claimed(now) = false immediately after Claim, want true")
	}
	if e.Claimed(fixed.Add(91 * time.Second)) {
		t.Error("Claimed(now+91s) = true, want false (CLAIM_TTL_S=90 elapsed)")
	}
}

func TestClaim_TwoDifferentRefsGetDistinctIDs(t *testing.T) {
	s := mustOpenStore(t)
	a, b := testRef("a@example.com"), testRef("b@example.com")
	claims, err := s.Claim([]AccountRef{a, b})
	if err != nil {
		t.Fatalf("Claim: %v", err)
	}
	if claims[a] == "" || claims[b] == "" || claims[a] == claims[b] {
		t.Fatalf("claims = %+v, want two distinct non-empty ids", claims)
	}
}

func TestRecord_SuccessFencedByClaimUpdatesLastGoodAndClearsFailureState(t *testing.T) {
	s := mustOpenStore(t)
	ref := testRef("a@example.com")

	// Seed a prior failure so success-clearing is actually exercised.
	if _, err := s.Record(map[AccountRef]FetchRecord{ref: {Error: "http-429", RetryAfterS: nil}}, nil); err != nil {
		t.Fatalf("seed failure Record: %v", err)
	}

	claims, err := s.Claim([]AccountRef{ref})
	if err != nil {
		t.Fatalf("Claim: %v", err)
	}
	usage := &provider.Usage{FiveHour: &provider.Window{Pct: 42}}
	plan := &Plan{NextPollAt: time.Now().Add(5 * time.Minute), IntervalS: 300}
	accepted, err := s.Record(map[AccountRef]FetchRecord{
		ref: {Usage: usage, Plan: plan},
	}, claims)
	if err != nil {
		t.Fatalf("Record: %v", err)
	}
	if !accepted[ref] {
		t.Fatalf("accepted[ref] = false, want true (fenced by a live claim)")
	}

	entries, _, err := s.Entries([]AccountRef{ref})
	if err != nil {
		t.Fatalf("Entries: %v", err)
	}
	e := entries[ref]
	if e.LastGood == nil || e.LastGood.FiveHour == nil || e.LastGood.FiveHour.Pct != 42 {
		t.Errorf("LastGood = %+v, want the recorded usage", e.LastGood)
	}
	if e.FetchedAt.IsZero() {
		t.Error("FetchedAt is zero after a successful Record")
	}
	if e.ConsecutiveFailures != 0 || e.LastError != "" {
		t.Errorf("failure state not cleared: ConsecutiveFailures=%d LastError=%q", e.ConsecutiveFailures, e.LastError)
	}
	if !e.BackoffUntil.IsZero() {
		t.Errorf("BackoffUntil = %v, want zero after success", e.BackoffUntil)
	}
	if e.NextPollAt.IsZero() || e.PollIntervalS == nil || *e.PollIntervalS != 300 {
		t.Errorf("plan not persisted: NextPollAt=%v PollIntervalS=%v", e.NextPollAt, e.PollIntervalS)
	}
	if e.Claimed(time.Now()) {
		t.Error("Claimed(now) = true after Record, want the claim cleared")
	}
}

func TestRecord_FailureIncrementsAndComputesBackoffViaNextPollAfterFetchError(t *testing.T) {
	s := mustOpenStore(t)
	ref := testRef("a@example.com")
	fixed := time.Date(2026, 8, 23, 0, 0, 0, 0, time.UTC)
	s.clock = func() time.Time { return fixed }

	retryAfter := 42.0
	accepted, err := s.Record(map[AccountRef]FetchRecord{
		ref: {Error: "http-429", RetryAfterS: &retryAfter},
	}, nil)
	if err != nil {
		t.Fatalf("Record: %v", err)
	}
	if !accepted[ref] {
		t.Fatal("accepted[ref] = false for an unfenced Record on a fresh row")
	}

	entries, _, err := s.Entries([]AccountRef{ref})
	if err != nil {
		t.Fatalf("Entries: %v", err)
	}
	e := entries[ref]
	if e.ConsecutiveFailures != 1 {
		t.Errorf("ConsecutiveFailures = %d, want 1", e.ConsecutiveFailures)
	}
	if e.LastError != "http-429" {
		t.Errorf("LastError = %q, want http-429", e.LastError)
	}
	if e.Last429At.IsZero() || !e.Last429At.Equal(fixed) {
		t.Errorf("Last429At = %v, want %v", e.Last429At, fixed)
	}

	// Cross-check: BackoffUntil must equal calling NextPollAfterFetchError
	// directly with consecutiveFailuresBefore=0 (the row's failure count
	// BEFORE this Record call) — proving Record's failure path really is
	// wired through the P4 planner, not a re-implementation that happens to
	// agree.
	want := NextPollAfterFetchError(fixed, 0, &FetchError{Kind: "http-429", RetryAfterS: &retryAfter})
	if !e.BackoffUntil.Equal(want) {
		t.Errorf("BackoffUntil = %v, want %v (from NextPollAfterFetchError)", e.BackoffUntil, want)
	}
	if !e.InBackoff(fixed) {
		t.Error("InBackoff(now) = false immediately after a 429, want true")
	}
	if !e.Recent429(fixed) {
		t.Error("Recent429(now) = false immediately after a 429, want true")
	}
}

func TestRecord_PermanentAuthErrorsAdvanceAuthDeadStrikesAndTokenDead(t *testing.T) {
	s := mustOpenStore(t)
	ref := testRef("a@example.com")

	for _, errKind := range []string{"invalid_grant", "no_refresh_token"} {
		t.Run(errKind, func(t *testing.T) {
			s2 := mustOpenStore(t)
			if _, err := s2.Record(map[AccountRef]FetchRecord{ref: {Error: errKind}}, nil); err != nil {
				t.Fatalf("Record: %v", err)
			}
			entries, _, err := s2.Entries([]AccountRef{ref})
			if err != nil {
				t.Fatalf("Entries: %v", err)
			}
			e := entries[ref]
			if e.AuthDeadStrikes != 1 {
				t.Errorf("AuthDeadStrikes = %d, want 1", e.AuthDeadStrikes)
			}
			if !e.TokenDead() {
				t.Error("TokenDead() = false, want true (AuthDeadStrikesThreshold=1)")
			}
		})
	}

	// A transient error must NOT advance the strike count.
	if _, err := s.Record(map[AccountRef]FetchRecord{ref: {Error: "timeout"}}, nil); err != nil {
		t.Fatalf("Record: %v", err)
	}
	entries, _, err := s.Entries([]AccountRef{ref})
	if err != nil {
		t.Fatalf("Entries: %v", err)
	}
	if e := entries[ref]; e.AuthDeadStrikes != 0 || e.TokenDead() {
		t.Errorf("transient failure must not advance AuthDeadStrikes: %+v", e)
	}
}

func TestRecord_SuccessAfterDeadStrikesResetsAuthDeadStrikes(t *testing.T) {
	s := mustOpenStore(t)
	ref := testRef("a@example.com")
	if _, err := s.Record(map[AccountRef]FetchRecord{ref: {Error: "invalid_grant"}}, nil); err != nil {
		t.Fatalf("Record failure: %v", err)
	}
	if _, err := s.Record(map[AccountRef]FetchRecord{ref: {Usage: nil}}, nil); err != nil {
		t.Fatalf("Record success: %v", err)
	}
	entries, _, err := s.Entries([]AccountRef{ref})
	if err != nil {
		t.Fatalf("Entries: %v", err)
	}
	if e := entries[ref]; e.AuthDeadStrikes != 0 || e.TokenDead() {
		t.Errorf("a success must reset AuthDeadStrikes/TokenDead: %+v", e)
	}
}

func TestRecord_StaleClaimIsFencedOut(t *testing.T) {
	s := mustOpenStore(t)
	ref := testRef("a@example.com")

	firstClaims, err := s.Claim([]AccountRef{ref})
	if err != nil {
		t.Fatalf("first Claim: %v", err)
	}
	secondClaims, err := s.Claim([]AccountRef{ref})
	if err != nil {
		t.Fatalf("second Claim: %v", err)
	}
	if firstClaims[ref] == secondClaims[ref] {
		t.Fatal("two Claim calls on the same ref produced the same claim id")
	}

	usage := &provider.Usage{FiveHour: &provider.Window{Pct: 10}}
	accepted, err := s.Record(map[AccountRef]FetchRecord{ref: {Usage: usage}}, firstClaims)
	if err != nil {
		t.Fatalf("Record with stale claim: %v", err)
	}
	if accepted[ref] {
		t.Error("accepted[ref] = true using the FIRST (superseded) claim id, want it fenced out")
	}

	accepted, err = s.Record(map[AccountRef]FetchRecord{ref: {Usage: usage}}, secondClaims)
	if err != nil {
		t.Fatalf("Record with live claim: %v", err)
	}
	if !accepted[ref] {
		t.Error("accepted[ref] = false using the CURRENT claim id, want it accepted")
	}
}

func TestRecord_UnfencedModeDefersOnlyToALiveClaim(t *testing.T) {
	s := mustOpenStore(t)
	ref := testRef("a@example.com")
	fixed := time.Date(2026, 8, 23, 0, 0, 0, 0, time.UTC)
	s.clock = func() time.Time { return fixed }

	if _, err := s.Claim([]AccountRef{ref}); err != nil {
		t.Fatalf("Claim: %v", err)
	}

	usage := &provider.Usage{FiveHour: &provider.Window{Pct: 5}}
	accepted, err := s.Record(map[AccountRef]FetchRecord{ref: {Usage: usage}}, nil)
	if err != nil {
		t.Fatalf("Record (unfenced, live claim): %v", err)
	}
	if accepted[ref] {
		t.Error("unfenced Record accepted despite a LIVE claim, want it deferred")
	}

	// Advance past the lease and try again unfenced: a crashed claimer's
	// leftover ticket must age out, not block forever.
	s.clock = func() time.Time { return fixed.Add(91 * time.Second) }
	accepted, err = s.Record(map[AccountRef]FetchRecord{ref: {Usage: usage}}, nil)
	if err != nil {
		t.Fatalf("Record (unfenced, expired claim): %v", err)
	}
	if !accepted[ref] {
		t.Error("unfenced Record deferred past the claim's TTL, want it accepted")
	}
}

func TestEntries_UnknownRefReturnsZeroValueNotError(t *testing.T) {
	s := mustOpenStore(t)
	ref := testRef("never-touched@example.com")
	entries, recovered, err := s.Entries([]AccountRef{ref})
	if err != nil || recovered {
		t.Fatalf("Entries: recovered=%v err=%v", recovered, err)
	}
	e := entries[ref]
	if e.LastGood != nil || !e.FetchedAt.IsZero() || e.Claimed(time.Now()) || e.InBackoff(time.Now()) || e.Recent429(time.Now()) || e.TokenDead() {
		t.Errorf("zero-value Entry has a non-empty predicate: %+v", e)
	}
}

func TestEntries_FreshRespectsServeTTLS(t *testing.T) {
	s := mustOpenStore(t)
	ref := testRef("a@example.com")
	fixed := time.Date(2026, 8, 23, 0, 0, 0, 0, time.UTC)
	s.clock = func() time.Time { return fixed }
	if _, err := s.Record(map[AccountRef]FetchRecord{ref: {Usage: &provider.Usage{FiveHour: &provider.Window{Pct: 1}}}}, nil); err != nil {
		t.Fatalf("Record: %v", err)
	}

	entries, _, err := s.Entries([]AccountRef{ref})
	if err != nil {
		t.Fatalf("Entries: %v", err)
	}
	e := entries[ref]
	if !e.Fresh(fixed.Add(time.Duration(ServeTTLS) * time.Second)) {
		t.Error("Fresh at exactly ServeTTLS should still be true (<=)")
	}
	if e.Fresh(fixed.Add(time.Duration(ServeTTLS)*time.Second + time.Second)) {
		t.Error("Fresh past ServeTTLS should be false")
	}
	if e.AgeS == nil {
		t.Fatal("AgeS = nil after a successful fetch")
	}
}

func TestPlanInput_WiresStoredEntryIntoPlanAfterFetch(t *testing.T) {
	// Demonstrates the P5 connection point end to end: Record a success with
	// a known interval, read it back as an Entry, build a PlanInput from it,
	// and confirm PlanAfterFetch actually uses PrevIntervalS/PrevUsage from
	// the store rather than some independently-tracked value.
	s := mustOpenStore(t)
	ref := testRef("a@example.com")
	t0 := time.Date(2026, 8, 23, 0, 0, 0, 0, time.UTC)
	s.clock = func() time.Time { return t0 }

	firstUsage := &provider.Usage{FiveHour: &provider.Window{Pct: 10}}
	if _, err := s.Record(map[AccountRef]FetchRecord{
		ref: {Usage: firstUsage, Plan: &Plan{NextPollAt: t0.Add(300 * time.Second), IntervalS: 300}},
	}, nil); err != nil {
		t.Fatalf("seed Record: %v", err)
	}

	entries, _, err := s.Entries([]AccountRef{ref})
	if err != nil {
		t.Fatalf("Entries: %v", err)
	}
	e := entries[ref]

	t1 := t0.Add(300 * time.Second)
	newUsage := &provider.Usage{FiveHour: &provider.Window{Pct: 40}} // moved >= MovementDeltaPct
	in := e.PlanInput(newUsage, true, 90, nil, t1)

	if in.PrevIntervalS == nil || *in.PrevIntervalS != 300 {
		t.Fatalf("PlanInput.PrevIntervalS = %v, want 300 from the stored plan", in.PrevIntervalS)
	}
	// A round trip through the persisted JSON file means this is a distinct
	// pointer from firstUsage, not the same allocation — compare by value.
	if in.PrevUsage == nil || in.PrevUsage.FiveHour == nil || in.PrevUsage.FiveHour.Pct != firstUsage.FiveHour.Pct {
		t.Fatalf("PlanInput.PrevUsage = %+v, want the stored LastGood (pct %v)", in.PrevUsage, firstUsage.FiveHour.Pct)
	}

	plan := PlanAfterFetch(in)
	// Movement (10 -> 40, delta 30 >= MovementDeltaPct) halves the base
	// interval (300/2=150), floored at MinIntervalS(180) -> 180.
	if plan.IntervalS != MinIntervalS {
		t.Errorf("PlanAfterFetch(in).IntervalS = %v, want %v (movement halves 300 to 150, floored at MinIntervalS)", plan.IntervalS, MinIntervalS)
	}
}

func TestReadRows_AbsentFileIsNotRecovery(t *testing.T) {
	s := mustOpenStore(t)
	_, recovered, err := s.Entries([]AccountRef{testRef("a@example.com")})
	if err != nil || recovered {
		t.Fatalf("a brand-new store must not report recovered=true: recovered=%v err=%v", recovered, err)
	}
}

func TestReadRows_CorruptFileRecoversToEmptyAndReportsIt(t *testing.T) {
	s := mustOpenStore(t)
	if err := os.WriteFile(s.path, []byte("{not valid json"), 0o600); err != nil {
		t.Fatalf("write corrupt file: %v", err)
	}
	entries, recovered, err := s.Entries([]AccountRef{testRef("a@example.com")})
	if err != nil {
		t.Fatalf("Entries on a corrupt file must not error: %v", err)
	}
	if !recovered {
		t.Error("recovered = false reading a corrupt file, want true")
	}
	if len(entries) != 1 {
		t.Fatalf("entries = %+v, want one zero-value entry", entries)
	}
}

func TestReadRows_ForeignSchemaVersionRecoversToEmpty(t *testing.T) {
	s := mustOpenStore(t)
	raw, err := json.Marshal(map[string]any{"schemaVersion": 2, "accounts": map[string]any{}})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if err := os.WriteFile(s.path, raw, 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	_, recovered, err := s.Entries([]AccountRef{testRef("a@example.com")})
	if err != nil || !recovered {
		t.Fatalf("a schemaVersion this code does not recognize must recover empty: recovered=%v err=%v", recovered, err)
	}
}

func TestWriteRows_AtomicAndPermission0600(t *testing.T) {
	s := mustOpenStore(t)
	ref := testRef("a@example.com")
	if _, err := s.Record(map[AccountRef]FetchRecord{ref: {Usage: &provider.Usage{FiveHour: &provider.Window{Pct: 1}}}}, nil); err != nil {
		t.Fatalf("Record: %v", err)
	}
	info, err := os.Stat(s.path)
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Errorf("permission = %o, want 0600", perm)
	}
	// No leftover temp files.
	entriesOnDisk, err := os.ReadDir(filepath.Dir(s.path))
	if err != nil {
		t.Fatalf("readdir: %v", err)
	}
	for _, de := range entriesOnDisk {
		if de.Name() != storeFileName && de.Name() != lockFileName {
			t.Errorf("unexpected leftover file %q in the store directory", de.Name())
		}
	}
}

func TestClearDeadToken_ResetsStrikesAndFailureState(t *testing.T) {
	s := mustOpenStore(t)
	ref := testRef("a@example.com")
	if _, err := s.Record(map[AccountRef]FetchRecord{ref: {Error: "invalid_grant"}}, nil); err != nil {
		t.Fatalf("Record: %v", err)
	}
	entries, _, err := s.Entries([]AccountRef{ref})
	if err != nil || !entries[ref].TokenDead() {
		t.Fatalf("setup: expected TokenDead() before ClearDeadToken, entries=%+v err=%v", entries, err)
	}

	if err := s.ClearDeadToken([]AccountRef{ref}); err != nil {
		t.Fatalf("ClearDeadToken: %v", err)
	}
	entries, _, err = s.Entries([]AccountRef{ref})
	if err != nil {
		t.Fatalf("Entries: %v", err)
	}
	if e := entries[ref]; e.TokenDead() || e.AuthDeadStrikes != 0 || e.ConsecutiveFailures != 0 || e.LastError != "" {
		t.Errorf("ClearDeadToken did not reset quarantine state: %+v", e)
	}
}

func TestClearDeadToken_NoOpOnUnknownRef(t *testing.T) {
	s := mustOpenStore(t)
	if err := s.ClearDeadToken([]AccountRef{testRef("never-touched@example.com")}); err != nil {
		t.Fatalf("ClearDeadToken on an untouched ref: %v", err)
	}
	if _, err := os.Stat(s.path); !os.IsNotExist(err) {
		t.Errorf("ClearDeadToken on an unknown ref must not create the store file (got err=%v)", err)
	}
}

func TestAccountRef_InvalidKeysAreRejected(t *testing.T) {
	s := mustOpenStore(t)
	bad := []AccountRef{
		{ProviderKey: "claude", AccountKey: "not-64-hex-chars"},
		{ProviderKey: "claude", AccountKey: ""},
		{ProviderKey: "../escape", AccountKey: testAccountKey("a@example.com")},
	}
	for _, ref := range bad {
		if _, err := ref.storeKey(); err == nil {
			t.Errorf("storeKey(%+v) = nil error, want rejection", ref)
		}
		if _, err := s.Claim([]AccountRef{ref}); err == nil {
			t.Errorf("Claim(%+v) = nil error, want rejection", ref)
		}
	}
}

func TestLiveClaim_NilVsExplicitZeroSentinel(t *testing.T) {
	now := time.Date(2026, 8, 23, 0, 0, 0, 0, time.UTC)

	// Never claimed at all (nil): falls back to lastAttemptAt.
	recentAttempt := now.Add(-5 * time.Second)
	if !liveClaim(nil, recentAttempt, now) {
		t.Error("liveClaim(nil, recent lastAttemptAt, now) = false, want true (legacy fallback)")
	}
	oldAttempt := now.Add(-20 * time.Second)
	if liveClaim(nil, oldAttempt, now) {
		t.Error("liveClaim(nil, old lastAttemptAt, now) = true, want false (past legacyClaimTTLS)")
	}

	// Explicitly cleared (non-nil pointer at a past instant, e.g. Record's
	// 0.0 sentinel): must NOT fall back to lastAttemptAt even when
	// lastAttemptAt is very recent — this is exactly the distinction
	// Record's clear depends on (a fresh LastAttemptAt stamped in the same
	// operation that clears the claim must not read back as still claimed).
	clearedAt := time.Unix(0, 0).UTC()
	if liveClaim(&clearedAt, recentAttempt, now) {
		t.Error("liveClaim(explicit past ClaimUntil, recent lastAttemptAt, now) = true, want false")
	}

	future := now.Add(30 * time.Second)
	if !liveClaim(&future, oldAttempt, now) {
		t.Error("liveClaim(future ClaimUntil, ...) = false, want true")
	}
}
