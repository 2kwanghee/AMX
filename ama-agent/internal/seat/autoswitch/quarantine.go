package autoswitch

import (
	"encoding/json"
	"errors"
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

// ShouldRelease reports whether a currently-quarantined account should
// re-enter rotation this tick, ported from the status-recovery half of
// tsamx's _release_recovered_quarantines (autoswitch.py:707-741): once an
// account's usageStatus is no longer relogin_required, the dead lineage is
// gone (a fresh add/relogin replaced the credential) and it belongs back in
// candidate selection.
//
// This is narrower than the original in one respect: tsamx ALSO releases on
// a changed refresh-token fingerprint even while usageStatus still reports
// relogin_required transiently (autoswitch.py:726-728), because it reads
// the credential file directly. This package has no credential access (see
// package doc) — a caller holding a fingerprint may release on that signal
// too, on top of this function's status-only check.
func ShouldRelease(usageStatus string, currentlyQuarantined bool) bool {
	return currentlyQuarantined && usageStatus != seatusage.StatusReloginRequired
}

// QuarantineEntry is the persisted record for one quarantined slot —
// contract C12's `quarantine{slot:{email,...}}` shape (docs/design-notes/
// tsamx-rewrite-feasibility.md 계약 table, row "상태 파일"), same field
// names tsamx's autoswitch.py:696-702 writes (email/reason/at;
// refreshTokenFingerprint is tsamx-only — this package never reads
// credential material, see package doc, so it is omitted rather than
// faked).
type QuarantineEntry struct {
	Email  string    `json:"email"`
	Reason string    `json:"reason,omitempty"`
	At     time.Time `json:"at,omitempty"`
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

// ReadState reads the quarantine map from path. A missing file yields an
// empty map and a nil error (nothing quarantined yet — mirrors
// internal/tsamx.ExecBridge.ReadQuarantine's same convention for tsamx's own
// file, exec.go:374-376, and autoswitch.py's _read_state which treats any
// unreadable/malformed file as empty state, autoswitch.py:669-674).
func ReadState(path string) (map[string]QuarantineEntry, error) {
	blob, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return map[string]QuarantineEntry{}, nil
		}
		return map[string]QuarantineEntry{}, nil // unreadable/partial write -> empty, same convention as ExecBridge.ReadQuarantine
	}
	var sf stateFile
	if err := json.Unmarshal(blob, &sf); err != nil {
		return map[string]QuarantineEntry{}, nil
	}
	if sf.Quarantine == nil {
		return map[string]QuarantineEntry{}, nil
	}
	return sf.Quarantine, nil
}

// WriteState atomically persists the quarantine map to path: write to a
// sibling temp file, then os.Rename into place. This is the SAME write
// pattern tsamx's atomic_write_json uses (autoswitch.py module doc line 27,
// "mutated read-modify-write under a dedicated file lock") and that
// internal/tsamx/contract_test.go's TestContractReadQuarantineParsesAtomic
// RenameWrite exercises for tsamx's file — a rename is what lets a
// directory-level fsnotify watch (internal/scheduler/scheduler.go:247-258)
// treat the write as one atomic change rather than observing a
// partially-written file.
func WriteState(path string, quarantine map[string]QuarantineEntry) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	sf := stateFile{SchemaVersion: StateSchemaVersion, Quarantine: quarantine}
	blob, err := json.Marshal(sf)
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, blob, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
