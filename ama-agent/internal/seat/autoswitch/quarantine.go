package autoswitch

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	seatusage "github.com/2kwanghee/AMX/ama-agent/internal/seat/usage"
)

// ShouldQuarantine reports whether an account's P4 usage status
// (internal/seat/usage/expiry.go's StatusReloginRequired /
// StatusTokenExpired) means this engine must never activate it — the port
// of tsamx's invalid_grant quarantine trigger
// (autoswitch.py:_freshen_target:796-798, applied at
// autoswitch.py:1310-1312), narrowed exactly the way the P4 review already
// fixed for this boundary (design note P5 bullet, seat-engine-plan.md:
// 136-139; internal/seat/usage/expiry.go's JudgeIdleExpiry doc, "P4 review,
// C1"):
//
//   - StatusReloginRequired ("relogin_required"): the refresh-token lineage
//     is confirmed dead (or never had one) — quarantine.
//   - StatusTokenExpired ("token_expired"): routine, transient, and
//     self-healing on the next successful refresh — MUST NOT quarantine.
//     tsamx itself only ever quarantines on invalid_grant/no_refresh_token,
//     never on a plain expiry (autoswitch.py:796-798); conflating the two
//     was the exact defect flagged in P4's review.
//   - Anything else (ok, "", or a status this package doesn't recognize) —
//     not a quarantine signal.
func ShouldQuarantine(usageStatus string) bool {
	return usageStatus == seatusage.StatusReloginRequired
}

// ShouldRelease reports whether a quarantined slot should be released this
// tick, and why. Ported 1:1 from tsamx's _release_recovered_quarantines
// (autoswitch.py:707-741) — ONLY two triggers release a quarantine:
//
//   - the slot's current email is absent or differs from the quarantined
//     email ("account-replaced" — mirrors the original's
//     `if not email_now or email_now != entry.get("email")`, autoswitch.py:
//     719-722. This also covers the slot disappearing from the pool
//     entirely: `present=false` behaves exactly like `email_now` being
//     falsy);
//   - the slot's current refresh-token fingerprint differs from the one
//     recorded at quarantine time ("credentials-replaced", autoswitch.py:
//     725-728). An empty currentFingerprint means "not computed this tick"
//     (the caller has no fingerprint to offer) and never triggers a release
//     on its own — mirrors Python's `None != entry.get(...)` only firing
//     when a REAL fingerprint was computed and differs.
//
// # Corrected in review C1
//
// The first version of this function released a quarantine whenever the
// account's P4 usageStatus merely stopped reporting relogin_required — that
// path DOES NOT EXIST in the original. A dead refresh-token lineage does
// not become alive again just because the reported status changed to
// token_expired, unavailable, or unmeasured (""); only a REPLACED
// credential (re-login/re-add — detected via email or fingerprint change)
// proves the lineage is no longer dead. The status-based path let every
// quarantined-but-still-dead account release itself the moment its usage
// simply stopped being fetched, which is silent and wrong in the unsafe
// direction (an unreachable/dead account re-entering rotation).
func ShouldRelease(entry QuarantineEntry, currentEmail string, present bool, currentFingerprint string) (release bool, reason string) {
	if !present || currentEmail == "" || currentEmail != entry.Email {
		return true, "account-replaced"
	}
	if currentFingerprint != "" && currentFingerprint != entry.RefreshTokenFingerprint {
		return true, "credentials-replaced"
	}
	return false, ""
}

// QuarantineEntry is the persisted record for one quarantined slot —
// contract C12's `quarantine{slot:{email,...}}` shape (docs/design-notes/
// tsamx-rewrite-feasibility.md 계약 table, row "상태 파일"), same field
// names tsamx's autoswitch.py:696-702 writes (email/reason/at/
// refreshTokenFingerprint).
//
// Keyed (in State/Input.Quarantine maps) by profile.AccountKey(email) —
// see decide.go's Target and the package doc's "합류 설계" section for why,
// NOT by the account's pool slot Number the first version of this file
// used (review C4: a Number is only stable within one snapshot, and does
// not correspond to anything internal/seat/usage's store or
// internal/seat.Switcher key on).
type QuarantineEntry struct {
	Email  string `json:"email"`
	Reason string `json:"reason,omitempty"`
	// RefreshTokenFingerprint is oauth.credential_fingerprint's value at
	// quarantine time (autoswitch.py:693-694), needed by ShouldRelease's
	// credentials-replaced check. Empty when the caller never had one to
	// offer (this package never reads credential material itself — see
	// package doc).
	RefreshTokenFingerprint string `json:"refreshTokenFingerprint,omitempty"`
	// At is always set by Decide when it creates an entry (never the Go
	// zero value in practice), so the missing `,omitempty` effect below is
	// immaterial in this package's own writes — noted because
	// encoding/json's omitempty does NOT treat a zero-valued time.Time (or
	// any struct) as empty, so a bare `json:"at,omitempty"` tag here would
	// be a no-op that misleadingly implies suppression (review C5
	// "발견물"). Left untagged with omitempty rather than switched to a
	// pointer, since every real entry always carries a real At and there is
	// no "intentionally absent" case to represent.
	At time.Time `json:"at"`
}

// stateFile is the on-disk shape WriteState/ReadState use. schemaVersion
// mirrors tsamx's STATE_SCHEMA_VERSION (autoswitch.py:59) so a future reader
// already used to versioned pool state files needs no new convention.
type stateFile struct {
	SchemaVersion int                        `json:"schemaVersion"`
	Quarantine    map[string]QuarantineEntry `json:"quarantine"`
}

const StateSchemaVersion = 1

// StatePath returns THIS engine's own quarantine/cooldown state file path —
// deliberately NOT tsamx's <XDG_DATA_HOME>/tsamx/autoswitch_state.json.
//
// Why separate: contract C6 (feasibility doc) names that path as tsamx's,
// and internal/tsamx.ExecBridge.AutoStatePath/ReadQuarantine already own
// reading it for the scheduler's fsnotify watch (internal/scheduler/
// scheduler.go:244-289, watching specifically for tsamx's own atomic-rename
// writes). Two independent engines read-modify-writing the SAME quarantine
// file would race each other's writes with no shared lock between them —
// structurally the same shared-mutable-state hazard the design note calls
// out for the P4→P5 usage store boundary ("두 수집기가 동시에 발사하면 같은
// 예산을 이중 소비", seat-engine-plan.md P6 절) applied to on-disk state
// instead of a request budget. Giving this engine its own path under
// <dataHome>/ama-autoswitch/ instead of <dataHome>/tsamx/ makes that race
// structurally impossible rather than merely unlikely, at zero cost since
// nothing reads this path yet (this package is inert, per package doc).
func StatePath(dataHome string) string {
	return filepath.Join(dataHome, "ama-autoswitch", "state.json")
}

// ReadState reads the quarantine map from path.
//
// A MISSING file yields an empty map and a nil error — nothing quarantined
// yet (mirrors internal/tsamx.ExecBridge.ReadQuarantine's same convention
// for tsamx's own file, exec.go:374-376, and autoswitch.py's _read_state
// treating an absent file as empty state, autoswitch.py:669-674).
//
// A file that EXISTS but fails to parse (corrupt, truncated, wrong shape)
// now returns a non-nil error instead of silently swallowing to an empty
// map (review C5 "발견물": the first version's swallow-to-empty behavior
// meant a corrupted state file silently released every quarantined account
// — the opposite of ReadQuarantine's "missing = nothing quarantined"
// convention, which is safe only because a MISSING file genuinely never
// quarantined anything; a CORRUPT file might have, and the caller must
// decide explicitly (e.g. keep the last-known-good in-memory state) rather
// than have this function decide "assume nothing" on its behalf).
func ReadState(path string) (map[string]QuarantineEntry, error) {
	blob, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return map[string]QuarantineEntry{}, nil
		}
		return nil, fmt.Errorf("autoswitch: read state %s: %w", path, err)
	}
	var sf stateFile
	if err := json.Unmarshal(blob, &sf); err != nil {
		return nil, fmt.Errorf("autoswitch: state file %s is corrupt: %w", path, err)
	}
	if sf.Quarantine == nil {
		return map[string]QuarantineEntry{}, nil
	}
	return sf.Quarantine, nil
}

// WriteState atomically persists the quarantine map to path: write to a
// unique sibling temp file, then os.Rename into place. This is the SAME
// write pattern tsamx's atomic_write_json uses (autoswitch.py module doc
// line 27) and that internal/tsamx/contract_test.go's
// TestContractReadQuarantineParsesAtomicRenameWrite exercises for tsamx's
// file — a rename is what lets a directory-level fsnotify watch (internal/
// scheduler/scheduler.go:247-258) treat the write as one atomic change
// rather than observing a partially-written file.
//
// The temp file name is now unique per call (os.CreateTemp with a
// "state-*.tmp" pattern) rather than a single fixed "state.json.tmp" the
// first version used (review C5 "발견물": two concurrent WriteState calls —
// e.g. this tick's engine and a manual repair tool — sharing one fixed temp
// path could interleave their writes/renames and corrupt or lose one
// writer's update; a unique name per call makes that structurally
// impossible, matching why tsamx itself locks around its write, autoswitch.
// py module doc line 27, "mutated read-modify-write under a dedicated file
// lock" — this package has no cross-process lock of its own yet, so a
// unique temp name is the cheapest available safety net until one exists).
func WriteState(path string, quarantine map[string]QuarantineEntry) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	sf := stateFile{SchemaVersion: StateSchemaVersion, Quarantine: quarantine}
	blob, err := json.Marshal(sf)
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, "state-*.tmp")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	_, writeErr := tmp.Write(blob)
	closeErr := tmp.Close()
	if writeErr != nil {
		os.Remove(tmpPath)
		return writeErr
	}
	if closeErr != nil {
		os.Remove(tmpPath)
		return closeErr
	}
	if err := os.Rename(tmpPath, path); err != nil {
		os.Remove(tmpPath)
		return err
	}
	return nil
}
