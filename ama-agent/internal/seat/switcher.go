// Package seat implements the P3 "전환기" (Switcher) layer on top of
// internal/seat/profile's P2 store: the one place that flips a provider's
// active-profile pointer, so profile.Store.SetActive itself staying
// policy-free (see its doc) does not turn into an unenforced write path.
//
// Nothing in this package is wired into cmd/ama or the tsamx bridge (design
// note §3 P3: "배선하지 마라 ... 이번 커밋도 불활성이다"); the native seat
// engine stays opt-in behind a flag introduced later (P6), and today's
// default tsamx-bridge behavior is completely untouched by this package's
// existence.
package seat

import (
	"errors"
	"fmt"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
	"github.com/2kwanghee/AMX/ama-agent/internal/seat/profile"
)

// ErrNotAssigned is returned by Switch when targetKey is not present in the
// caller-supplied assignedKeys set. Owner-scope policy judgment itself (who
// is allowed to hold which account) is the server's job (design note §1, §2
// PolicyGuard) — this package enforces only the narrower, purely mechanical
// rule P2 review ⑤ asked for: never activate an account the server did not
// hand this agent as a candidate, full stop, with no local judgment about
// WHY it is or is not a candidate.
var ErrNotAssigned = errors.New("seat: switch target is not in the server-assigned set")

// ErrNotReady is returned by Switch when targetKey's profile has no usable
// credential at all (profile.StateAbsent) — there is nothing to activate.
var ErrNotReady = errors.New("seat: switch target has no staged credential")

// ActiveInfo describes the profile a provider currently has active.
type ActiveInfo struct {
	// AccountKey is the profile.AccountKey-shaped identifier of the active
	// profile. Populated even when Email could not be resolved.
	AccountKey string
	// ConfigDir is the profile's config-home path (profile.Store.GetActive's
	// third return value).
	ConfigDir string
	// Email is the identity Driver.Identity(ConfigDir) read back, or "" when
	// it could not be resolved (e.g. the profile was staged by something that
	// never recorded an identity, or a driver-specific read error). A caller
	// that needs to know WHY should call Driver.Identity itself; Switcher's
	// contract is best-effort convenience, not a second error channel.
	Email string
}

// Switcher is the sole intended writer of a provider's active pointer.
// profile.Store.SetActive performs no check on its own (by design — see its
// doc); every write in this package goes through Switch, which enforces the
// one policy P3 owns (⑤: never activate outside the server-assigned set) and
// the one bookkeeping guarantee P3 owns (①: an observed-but-unacknowledged
// credential rotation is reconciled, not treated as not-ready).
type Switcher struct {
	// Store is the profile store this Switcher's provider's profiles live in.
	Store *profile.Store
	// Driver owns the vendor-specific credential/identity knowledge (see
	// provider.Driver). Its Name() determines which provider this Switcher
	// operates on.
	Driver provider.Driver

	// testAfterSetActive, when non-nil, runs immediately after SetActive
	// succeeds and before Switch's C10 read-back. TEST-ONLY (unexported, only
	// this package's own tests can set it): SetActive's atomic rename makes a
	// genuine C10 violation vanishingly rare and impossible to force from
	// outside without a flaky real race, so tests use this hook to
	// deterministically reproduce "something else changed the pointer/profile
	// in the gap" and exercise the rollback path below. New never sets it, so
	// it is always nil in production.
	testAfterSetActive func()
}

// New returns a Switcher over store for drv's provider.
func New(store *profile.Store, drv provider.Driver) *Switcher {
	return &Switcher{Store: store, Driver: drv}
}

// Active resolves the profile currently active for this Switcher's provider.
// It propagates profile.Store.GetActive's errors UNCHANGED (ErrNoActive:
// nothing has ever been activated; ErrActiveMissing: the pointer names a
// Remove()d profile — see Repair) so a caller can tell the three outcomes
// apart. Email resolution is best-effort: a Driver.Identity failure does not
// turn a successful GetActive into an error, it just leaves Email empty.
func (sw *Switcher) Active() (ActiveInfo, error) {
	key, dir, err := sw.Store.GetActive(sw.Driver.Name())
	if err != nil {
		return ActiveInfo{AccountKey: key}, err
	}
	email, _ := sw.Driver.Identity(dir)
	return ActiveInfo{AccountKey: key, ConfigDir: dir, Email: email}, nil
}

// Switch activates targetKey for this Switcher's provider.
//
//   - ⑤: targetKey MUST be present in assignedKeys (the accounts the server
//     has told this agent it may hold for this provider right now) or Switch
//     refuses with ErrNotAssigned. This package does not itself decide who
//     owns what; it only refuses to go outside what the caller says the
//     server assigned.
//   - ①: if targetKey's profile.State is StateRotated (a healthy in-place
//     credential rotation the marker has not caught up to yet), Switch
//     reconciles it first so activating a freshly-rotated account is never
//     blocked by a stale marker. StateAbsent refuses with ErrNotReady;
//     StateIncomplete (a logged-out/blank credential) is NOT refused here —
//     that judgment belongs to whatever selects candidates upstream (P5's
//     AutoSwitch), not to this mechanical pointer flip.
//   - C10 (design note "tsamx-rewrite-feasibility.md" 계약 표, 동기성): before
//     returning, Switch re-reads Active() and requires it to report
//     targetKey. profile.Store.SetActive's atomic rename already makes this
//     true in every real case; the check exists so a violation is a loud
//     error here rather than a silent contract break a caller discovers
//     later.
//   - Rollback on a C10 violation (adversarial review F6): the pointer's
//     PREVIOUS value is read before SetActive ever writes, so that if the
//     read-back disagrees, Switch attempts to restore that previous value
//     rather than leaving the provider pointed at nothing/something unread-
//     backable. Best-effort only — reaching this branch at all means
//     something already unusual happened (a concurrent writer, a filesystem
//     that lied about a completed rename), so the rollback write can fail for
//     the same reason the forward one's read-back just did; the returned
//     error says which case occurred (rolled back / rollback also failed / no
//     previous value existed to roll back to).
func (sw *Switcher) Switch(targetKey string, assignedKeys []string) (ActiveInfo, error) {
	if !containsKey(assignedKeys, targetKey) {
		return ActiveInfo{}, fmt.Errorf("%w: %s", ErrNotAssigned, targetKey)
	}
	st, err := sw.Store.State(sw.Driver, targetKey)
	if err != nil {
		return ActiveInfo{}, err
	}
	switch st {
	case profile.StateAbsent:
		return ActiveInfo{}, fmt.Errorf("%w: %s", ErrNotReady, targetKey)
	case profile.StateRotated:
		if _, err := sw.Store.Reconcile(sw.Driver, targetKey); err != nil {
			return ActiveInfo{}, fmt.Errorf("seat: reconcile %s before switch: %w", targetKey, err)
		}
	}

	// Captured BEFORE the write so a failed read-back below has something to
	// roll back to. previousErr set (ErrNoActive/ErrActiveMissing/other) means
	// there was nothing safe to restore.
	previousKey, _, previousErr := sw.Store.GetActive(sw.Driver.Name())

	if err := sw.Store.SetActive(sw.Driver.Name(), targetKey); err != nil {
		return ActiveInfo{}, err
	}
	if sw.testAfterSetActive != nil {
		sw.testAfterSetActive()
	}

	info, err := sw.Active()
	if err == nil && info.AccountKey == targetKey {
		return info, nil
	}
	var violation error
	if err != nil {
		violation = fmt.Errorf("seat: switch to %s did not take effect (contract C10 violated): %w", targetKey, err)
	} else {
		violation = fmt.Errorf("seat: switch to %s read back %s instead (contract C10 violated)", targetKey, info.AccountKey)
	}
	if previousErr != nil || previousKey == "" {
		return ActiveInfo{}, fmt.Errorf("%w; no previous active to roll back to (prior lookup: %v)", violation, previousErr)
	}
	if rerr := sw.Store.SetActive(sw.Driver.Name(), previousKey); rerr != nil {
		return ActiveInfo{}, fmt.Errorf("%w; rollback to previous active %s also failed: %v", violation, previousKey, rerr)
	}
	return ActiveInfo{}, fmt.Errorf("%w; rolled back to previous active %s", violation, previousKey)
}

// Repair is the orphaned-active-pointer handler P2 review ④ asked P3 for:
// profile.Store.Remove never touches the active pointer, so removing the
// currently-active profile leaves GetActive reporting ErrActiveMissing
// forever until something notices.
//
//   - A healthy pointer (Active succeeds) or no pointer at all (ErrNoActive)
//     is left exactly alone — Repair returns whatever Active returned,
//     unchanged. Repair's whole job is the ErrActiveMissing case; it is not a
//     general-purpose "make sure something is active" call.
//   - An orphaned pointer (ErrActiveMissing) with a non-empty assignedKeys is
//     repointed to the first READY candidate, tried in order, via Switch — so
//     the same ⑤/① checks Switch always applies still apply to the repair
//     target; Repair grants no bypass. A candidate that Switch refuses with
//     ErrNotReady (profile.StateAbsent — nothing staged there at all) is
//     skipped in favor of the next one (adversarial review F6): a stale or
//     not-yet-staged entry at assignedKeys[0] must not make Repair give up
//     when a later candidate is perfectly usable. Any OTHER error from Switch
//     (including ErrNotAssigned, which should be structurally impossible here
//     since every candidate comes from assignedKeys itself) is NOT treated as
//     skippable — it propagates immediately rather than silently trying more
//     candidates, so an unexpected failure mode is never masked as "just try
//     the next one".
//   - An orphaned pointer with an EMPTY assignedKeys, or where NONE of the
//     candidates are ready, fails clearly rather than guessing: the former
//     returns the original ErrActiveMissing unchanged, the latter returns an
//     error wrapping the last candidate's ErrNotReady, so the caller can
//     re-provision or alert either way.
func (sw *Switcher) Repair(assignedKeys []string) (ActiveInfo, error) {
	info, err := sw.Active()
	if err == nil {
		return info, nil
	}
	if !errors.Is(err, profile.ErrActiveMissing) {
		return ActiveInfo{}, err
	}
	if len(assignedKeys) == 0 {
		return ActiveInfo{}, err
	}
	lastErr := err
	for _, candidate := range assignedKeys {
		info, serr := sw.Switch(candidate, assignedKeys)
		if serr == nil {
			return info, nil
		}
		if !errors.Is(serr, ErrNotReady) {
			return ActiveInfo{}, serr
		}
		lastErr = serr
	}
	return ActiveInfo{}, fmt.Errorf("seat: no assigned candidate is ready to repair the orphaned pointer: %w", lastErr)
}

func containsKey(keys []string, target string) bool {
	for _, k := range keys {
		if k == target {
			return true
		}
	}
	return false
}
