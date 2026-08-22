package seat

import (
	"errors"
	"strings"
	"testing"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
	"github.com/2kwanghee/AMX/ama-agent/internal/provider/claude"
	"github.com/2kwanghee/AMX/ama-agent/internal/seat/profile"
)

func openStore(t *testing.T) *profile.Store {
	t.Helper()
	s, err := profile.Open(t.TempDir())
	if err != nil {
		t.Fatalf("profile.Open: %v", err)
	}
	return s
}

func sampleCredential(refreshToken string) []byte {
	return []byte(`{"claudeAiOauth":{"accessToken":"at-` + refreshToken + `","refreshToken":"` + refreshToken + `"}}`)
}

func stageAccount(t *testing.T, s *profile.Store, drv provider.Driver, email string) string {
	t.Helper()
	key := profile.AccountKey(email)
	if err := s.Stage(drv, key, sampleCredential("rt-"+email), provider.AddMeta{Email: email}); err != nil {
		t.Fatalf("Stage(%s): %v", email, err)
	}
	return key
}

// --- Active -----------------------------------------------------------

func TestActivePropagatesErrNoActive(t *testing.T) {
	s := openStore(t)
	sw := New(s, claude.New())
	_, err := sw.Active()
	if !errors.Is(err, profile.ErrNoActive) {
		t.Fatalf("Active with no pointer written: err = %v, want ErrNoActive", err)
	}
}

func TestActivePropagatesErrActiveMissing(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	sw := New(s, drv)
	key := stageAccount(t, s, drv, "orphan@example.com")
	if err := s.SetActive(drv.Name(), key); err != nil {
		t.Fatalf("SetActive: %v", err)
	}
	if err := s.Remove(drv.Name(), key); err != nil {
		t.Fatalf("Remove: %v", err)
	}
	_, err := sw.Active()
	if !errors.Is(err, profile.ErrActiveMissing) {
		t.Fatalf("Active after Remove()ing the active profile: err = %v, want ErrActiveMissing", err)
	}
}

func TestActiveResolvesEmailViaIdentity(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	sw := New(s, drv)
	key := stageAccount(t, s, drv, "resolved@example.com")
	if err := s.SetActive(drv.Name(), key); err != nil {
		t.Fatalf("SetActive: %v", err)
	}
	info, err := sw.Active()
	if err != nil {
		t.Fatalf("Active: %v", err)
	}
	if info.AccountKey != key {
		t.Fatalf("AccountKey = %q, want %q", info.AccountKey, key)
	}
	if info.Email != "resolved@example.com" {
		t.Fatalf("Email = %q, want %q", info.Email, "resolved@example.com")
	}
}

// --- Switch: ⑤ owner-scope enforcement ---------------------------------

func TestSwitchRejectsAccountOutsideAssignedSet(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	sw := New(s, drv)
	key := stageAccount(t, s, drv, "unassigned@example.com")

	_, err := sw.Switch(key, []string{profile.AccountKey("someone-else@example.com")})
	if !errors.Is(err, ErrNotAssigned) {
		t.Fatalf("Switch outside assigned set: err = %v, want ErrNotAssigned", err)
	}
	// Must not have taken effect.
	if _, _, gerr := s.GetActive(drv.Name()); !errors.Is(gerr, profile.ErrNoActive) {
		t.Fatalf("a rejected Switch must not touch the active pointer: GetActive err = %v", gerr)
	}
}

func TestSwitchSucceedsForAssignedAccount(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	sw := New(s, drv)
	key := stageAccount(t, s, drv, "assigned@example.com")

	info, err := sw.Switch(key, []string{key})
	if err != nil {
		t.Fatalf("Switch: %v", err)
	}
	if info.AccountKey != key {
		t.Fatalf("AccountKey = %q, want %q", info.AccountKey, key)
	}
}

// --- Switch: readiness --------------------------------------------------

func TestSwitchRejectsAbsentProfile(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	sw := New(s, drv)
	key := profile.AccountKey("never-staged@example.com")
	if _, err := s.Create(drv.Name(), key, profile.Template{}); err != nil {
		t.Fatalf("Create: %v", err)
	}

	_, err := sw.Switch(key, []string{key})
	if !errors.Is(err, ErrNotReady) {
		t.Fatalf("Switch to a Create()-only (never Stage()d) profile: err = %v, want ErrNotReady", err)
	}
}

// TestSwitchReconcilesRotatedProfileInstead is P2 review ①'s exact concern
// applied to Switch: a profile whose credential was rotated in place by the
// vendor's own runner (profile.StateRotated) must switch successfully, and
// the marker must be re-stamped rather than left stale, or a caller (P5's
// AutoSwitch) that gates on profile.State would keep seeing this healthy
// account as needing attention forever.
func TestSwitchReconcilesRotatedProfileInstead(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	sw := New(s, drv)
	key := stageAccount(t, s, drv, "rotates@example.com")
	dir, err := s.ProfileDir(drv.Name(), key)
	if err != nil {
		t.Fatalf("ProfileDir: %v", err)
	}
	if err := drv.StageCredential(dir, sampleCredential("rt-rotated"), provider.AddMeta{Email: "rotates@example.com"}); err != nil {
		t.Fatalf("simulate in-place rotation: %v", err)
	}
	if st, err := s.State(drv, key); err != nil || st != profile.StateRotated {
		t.Fatalf("precondition: State = (%v, %v), want (StateRotated, nil)", st, err)
	}

	if _, err := sw.Switch(key, []string{key}); err != nil {
		t.Fatalf("Switch on a rotated-but-healthy profile: %v", err)
	}
	if st, err := s.State(drv, key); err != nil || st != profile.StateStaged {
		t.Fatalf("State after Switch = (%v, %v), want (StateStaged, nil) — Switch must reconcile the rotation", st, err)
	}
}

// --- Switch: C10 synchronous completion --------------------------------

func TestSwitchIsSynchronouslyVisible(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	sw := New(s, drv)
	first := stageAccount(t, s, drv, "first@example.com")
	second := stageAccount(t, s, drv, "second@example.com")

	if _, err := sw.Switch(first, []string{first, second}); err != nil {
		t.Fatalf("Switch(first): %v", err)
	}
	if _, err := sw.Switch(second, []string{first, second}); err != nil {
		t.Fatalf("Switch(second): %v", err)
	}
	// C10: the very next Active() call (standing in for tsamx's "직후 list가
	// 새 active를 반환") must report the SECOND switch, not the first.
	info, err := sw.Active()
	if err != nil {
		t.Fatalf("Active: %v", err)
	}
	if info.AccountKey != second {
		t.Fatalf("Active after Switch(second) = %q, want %q (contract C10)", info.AccountKey, second)
	}
}

// --- Repair (P2 review ④) ------------------------------------------------

func TestRepairIsNoOpWhenPointerHealthy(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	sw := New(s, drv)
	key := stageAccount(t, s, drv, "healthy@example.com")
	if _, err := sw.Switch(key, []string{key}); err != nil {
		t.Fatalf("Switch: %v", err)
	}

	info, err := sw.Repair([]string{key})
	if err != nil {
		t.Fatalf("Repair on a healthy pointer: %v", err)
	}
	if info.AccountKey != key {
		t.Fatalf("Repair changed the active account: got %q, want unchanged %q", info.AccountKey, key)
	}
}

func TestRepairPropagatesErrNoActiveUnchanged(t *testing.T) {
	s := openStore(t)
	sw := New(s, claude.New())
	_, err := sw.Repair([]string{profile.AccountKey("candidate@example.com")})
	if !errors.Is(err, profile.ErrNoActive) {
		t.Fatalf("Repair with no pointer ever set: err = %v, want ErrNoActive (never-set is not orphaned)", err)
	}
}

func TestRepairFailsClearlyWithNoCandidates(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	sw := New(s, drv)
	key := stageAccount(t, s, drv, "orphan2@example.com")
	if _, err := sw.Switch(key, []string{key}); err != nil {
		t.Fatalf("Switch: %v", err)
	}
	if err := s.Remove(drv.Name(), key); err != nil {
		t.Fatalf("Remove: %v", err)
	}

	_, err := sw.Repair(nil)
	if !errors.Is(err, profile.ErrActiveMissing) {
		t.Fatalf("Repair with no candidates on an orphaned pointer: err = %v, want ErrActiveMissing (fail clearly, do not guess)", err)
	}
	// Must not have silently repointed to anything.
	if _, _, gerr := s.GetActive(drv.Name()); !errors.Is(gerr, profile.ErrActiveMissing) {
		t.Fatalf("Repair with no candidates must leave the orphaned pointer untouched: GetActive err = %v", gerr)
	}
}

func TestRepairRepointsToCandidateOnOrphan(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	sw := New(s, drv)
	gone := stageAccount(t, s, drv, "gone@example.com")
	replacement := stageAccount(t, s, drv, "replacement@example.com")
	if _, err := sw.Switch(gone, []string{gone, replacement}); err != nil {
		t.Fatalf("Switch(gone): %v", err)
	}
	if err := s.Remove(drv.Name(), gone); err != nil {
		t.Fatalf("Remove: %v", err)
	}

	info, err := sw.Repair([]string{replacement})
	if err != nil {
		t.Fatalf("Repair: %v", err)
	}
	if info.AccountKey != replacement {
		t.Fatalf("Repair repointed to %q, want the candidate %q", info.AccountKey, replacement)
	}
	// C10 still holds through Repair's Switch call.
	if key, _, err := s.GetActive(drv.Name()); err != nil || key != replacement {
		t.Fatalf("GetActive after Repair = (%q, %v), want (%q, nil)", key, err, replacement)
	}
}

// TestRepairStillEnforcesAssignedSet confirms Repair grants no bypass of ⑤:
// repointing to a candidate that is not ALSO itself present in the same
// assignedKeys list Repair was given would defeat the owner-scope guarantee
// Switch enforces everywhere else.
func TestRepairStillEnforcesAssignedSet(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	sw := New(s, drv)
	gone := stageAccount(t, s, drv, "gone2@example.com")
	if _, err := sw.Switch(gone, []string{gone}); err != nil {
		t.Fatalf("Switch(gone): %v", err)
	}
	if err := s.Remove(drv.Name(), gone); err != nil {
		t.Fatalf("Remove: %v", err)
	}
	// The candidate profile does not even exist -- Repair's Switch(candidate,
	// [candidate]) must fail on readiness (ErrNotReady), not silently
	// activate a nonexistent profile.
	nonexistent := profile.AccountKey("phantom@example.com")
	_, err := sw.Repair([]string{nonexistent})
	if !errors.Is(err, ErrNotReady) {
		t.Fatalf("Repair to a candidate with no staged credential: err = %v, want ErrNotReady", err)
	}
}

// --- F6 (adversarial review): C10 rollback + Repair candidate skip ------

// TestSwitchRollsBackWhenReadBackFailsAfterSetActive deterministically
// reproduces a C10 violation via testAfterSetActive (a genuine one needs a
// real filesystem race that would be flaky to assert on) and checks Switch
// restores the PREVIOUS active pointer rather than leaving it dangling at a
// profile that no longer resolves.
func TestSwitchRollsBackWhenReadBackFailsAfterSetActive(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	sw := New(s, drv)
	first := stageAccount(t, s, drv, "rollback-first@example.com")
	second := stageAccount(t, s, drv, "rollback-second@example.com")
	if _, err := sw.Switch(first, []string{first, second}); err != nil {
		t.Fatalf("Switch(first): %v", err)
	}

	// Simulate something removing `second`'s profile in the gap between
	// SetActive(second) succeeding and Switch's own C10 read-back.
	sw.testAfterSetActive = func() {
		if err := s.Remove(drv.Name(), second); err != nil {
			t.Fatalf("Remove(second) in test hook: %v", err)
		}
	}

	_, err := sw.Switch(second, []string{first, second})
	if err == nil {
		t.Fatal("Switch must report an error when the C10 read-back fails")
	}
	if !strings.Contains(err.Error(), "rolled back to previous active") {
		t.Fatalf("error = %v, want it to say the pointer was rolled back", err)
	}

	key, _, gerr := s.GetActive(drv.Name())
	if gerr != nil || key != first {
		t.Fatalf("after a failed switch, GetActive = (%q, %v), want (%q, nil) — rollback must restore the previous active", key, gerr, first)
	}
}

// TestSwitchWithNoPreviousActiveReportsNoRollbackTarget covers the
// first-ever-switch case: there is nothing to roll back to, and Switch must
// say so rather than claiming a rollback that did not happen.
func TestSwitchWithNoPreviousActiveReportsNoRollbackTarget(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	sw := New(s, drv)
	target := stageAccount(t, s, drv, "no-previous@example.com")

	sw.testAfterSetActive = func() {
		if err := s.Remove(drv.Name(), target); err != nil {
			t.Fatalf("Remove(target) in test hook: %v", err)
		}
	}

	_, err := sw.Switch(target, []string{target})
	if err == nil {
		t.Fatal("Switch must report an error when the C10 read-back fails")
	}
	if !strings.Contains(err.Error(), "no previous active to roll back to") {
		t.Fatalf("error = %v, want it to say there was no previous active to roll back to", err)
	}
}

// TestRepairSkipsNotReadyCandidateAndTriesNext is F6's Repair half: a stale
// or never-staged entry earlier in assignedKeys must not make Repair give up
// when a later candidate is perfectly usable.
func TestRepairSkipsNotReadyCandidateAndTriesNext(t *testing.T) {
	s := openStore(t)
	drv := claude.New()
	sw := New(s, drv)
	gone := stageAccount(t, s, drv, "gone3@example.com")
	if _, err := sw.Switch(gone, []string{gone}); err != nil {
		t.Fatalf("Switch(gone): %v", err)
	}
	if err := s.Remove(drv.Name(), gone); err != nil {
		t.Fatalf("Remove: %v", err)
	}

	notReady := profile.AccountKey("not-ready@example.com")
	if _, err := s.Create(drv.Name(), notReady, profile.Template{}); err != nil {
		t.Fatalf("Create: %v", err)
	}
	ready := stageAccount(t, s, drv, "ready@example.com")

	info, err := sw.Repair([]string{notReady, ready})
	if err != nil {
		t.Fatalf("Repair: %v", err)
	}
	if info.AccountKey != ready {
		t.Fatalf("Repair = %q, want it to skip the not-ready candidate and land on %q", info.AccountKey, ready)
	}
}
