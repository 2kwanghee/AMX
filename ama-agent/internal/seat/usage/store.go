// This file ports tsamx's per-account usage state table
// (tsamx/src/tsamx/usage_store.py) to Go — the P5 선결 조건 #2 (design note
// docs/design-notes/seat-engine-plan.md, P5: "사용량 상태 저장소(선결)"). P4's
// PlanAfterFetch/NextPollAfterFetchError (policy.go) are pure functions: they
// need a caller to hold the previous observation, the previous interval, the
// recent-429 flag, and the backoff-until instant across calls. tsamx keeps
// that state in cache/usage.json plus a claim/lease so several processes
// sharing one account's polling budget do not double-fetch. This file is that
// state layer for the new engine — nothing in cmd/ama or the tsamx bridge
// constructs a Store from here; it stays exactly as inert as P4's collector.
//
// # File location — deliberately NOT tsamx's cache/usage.json
//
// This store never reads or writes tsamx's cache/usage.json. Reasons, per the
// P5 task brief:
//
//  1. Schema collision risk: tsamx's schemaVersion 2 rows are keyed by a
//     reusable numbered SLOT (see below) and carry fields
//     (email/organizationUuid identity guards) this store has no use for;
//     sharing the file would mean either engine's writes are junk to the
//     other's reader, or a migration/versioning scheme neither engine
//     currently has reason to build.
//  2. tsamx's own e2e suite treats cache/usage.json's exact shape as a
//     contract (P0's golden-test spirit — "이 계약을 건드리면 tsamx가
//     깨진다"); writing to it from a second, independently-evolving engine
//     risks breaking that contract by a change tsamx's own tests would never
//     catch.
//  3. Mutual pollution: this package's Fetch (collector.go) and tsamx's own
//     polling already share exactly one real resource — the usage endpoint's
//     rolling request budget per account (collector.go's package doc, "M4
//     double-consumption risk") — but that risk is NOT solved by sharing the
//     cache file; sharing the file would only add corrupted-state risk on
//     top of the already-open budget risk, with no offsetting benefit (see
//     P6 in the design note for the real fix: shadow-only reads of tsamx's
//     cache until this engine owns real polling).
//
// This store's rows instead key directly on profile.AccountKey — a one-way
// hash of the account's own (lowercased, trimmed) email — combined with the
// provider key. This is a structural difference from tsamx worth stating
// explicitly: tsamx's usage_store.py keys rows by a REUSABLE NUMBERED SLOT
// (e.g. "3"), so a row can legitimately belong to a DIFFERENT account after a
// slot is freed and reassigned — which is exactly why UsageStore.entries()
// carries an identity guard (_matches: email + organizationUuid must match
// the caller's current view of that slot) before trusting any stored row.
// This store has no such reuse: an AccountRef's key is derived from the
// account's own identity, so the row for a given key can never have been
// written by a different account in the first place. The identity-guard
// mechanism is therefore deliberately NOT ported — porting it here would be
// dead code guarding against a slot-reuse condition this store's key scheme
// makes structurally impossible.
//
// # Locking protocol — ported unchanged
//
// Same three-phase discipline as usage_store.py's module doc (never holds
// the lock across network I/O):
//
//	(a) Reserve: lock -> re-check eligibility -> stamp claimUntil on the rows
//	    that pass -> unlock;
//	(b) fetch: the caller's own network round trip, with NO lock held;
//	(c) Record: lock -> re-read, merge outcomes fenced by the claim ids
//	    Reserve handed out, clear the claim, write -> unlock.
//
// The lock lives at <stateDir>/usage/.usage.lock — a different file from
// both P2's per-profile locks (<stateDir>/profiles/<provider>/
// <accountKey>.lock) and the deliver lock (<configDir>/.amx-deliver.lock):
// this store's rows carry no credential material and are wholly unrelated to
// either critical section, so sharing a lock file with them would only
// create a false dependency between three otherwise-independent locks.
//
// # Scope boundary — what this file does NOT build
//
// tsamx's usage_store.py's reserve() additionally gates eligibility on
// nextPollAt/pollIntervalS ("poll-due") under two caller modes
// (respect_plans/repair_overslept) — deciding WHICH of several already-
// eligible candidates is due for a fetch RIGHT NOW under an adaptive
// cadence. That half is SCHEDULING policy and belongs to the P5 AutoSwitch
// engine this task is explicitly forbidden from building (design note P5's
// threshold/cooldown/hysteresis/selection-strategy section). Store.Reserve
// below ports only reserve()'s other half — the four EXCLUSIVITY conditions
// that exist purely to stop two collectors from double-fetching the SAME
// row (not claimed, not backing off, not quarantined, not already fresh) —
// plus `record()` (here: Store.Record) and the read model's predicates
// (fresh/in_backoff/recent_429/claimed/tokenDead). See Reserve's own doc for
// exactly which usage_store.py function this replaces and why (adversarial
// review A2).
package usage

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/fslock"
	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
)

// ClaimTTLS is the in-flight fetch lease window, ported unchanged from
// usage_store.py's CLAIM_TTL_S (:62): long enough to cover a bounded refresh
// + usage round trip, the collector's own stagger, and executor queueing, so
// another surface cannot reclaim a request that is still in flight; short
// enough that a crashed claimer's leftover lease still ages out well inside
// the provider-safe polling interval.
const ClaimTTLS = 90.0

// legacyClaimTTLS mirrors usage_store.py's LEGACY_CLAIM_TTL_S (:63) — the
// fallback window liveClaim uses when a row carries no claimUntil at all
// (ClaimUntil == nil), based on lastAttemptAt instead. Ported for structural
// parity with the ORIGINAL's predicate even though, unlike tsamx, no older
// writer of THIS store's schema can ever exist yet (this file is v1 from
// day one): a future schema evolution of this same store could still leave
// rows without a claimUntil, and liveClaim's contract should not need to
// change to cover that.
const legacyClaimTTLS = 10.0

// AuthDeadStrikesThreshold mirrors usage_store.py's AUTH_DEAD_STRIKES (:227):
// the number of consecutive permanent-auth failures (see permanentAuthErrors)
// after which Entry.TokenDead reports the refresh-token lineage as dead. tsamx
// sets this to 1 deliberately (a server-rejected grant is already
// definitive); raising it trades a slower verdict for a buffer against a
// one-off misclassification — not needed here, so it stays at parity.
const AuthDeadStrikesThreshold = 1

// permanentAuthErrors mirrors usage_store.py's PERMANENT_AUTH_ERRORS (:234):
// the only FetchRecord.Error values that prove a stored credential is
// unusable rather than merely a transient fetch failure. Only these advance
// Entry.AuthDeadStrikes. Values match this package's own refresh.go
// RefreshOutcome.Error vocabulary ("invalid_grant", "no_refresh_token"), so a
// caller that folds a failed refresh attempt into a FetchRecord gets the
// same permanence judgment tsamx makes. Deliberately does NOT include
// refresh.go's "malformed_credential" (adversarial review A5): a credential
// this package merely failed to PARSE is no evidence the account itself is
// unusable, and must never advance the quarantine strike count the way a
// server-confirmed dead refresh-token lineage does.
var permanentAuthErrors = map[string]bool{
	"invalid_grant":    true,
	"no_refresh_token": true,
}

const (
	storeSchemaVersion = 1
	storeSubdir        = "usage"
	storeFileName      = "usage.json"
	lockFileName       = ".usage.lock"
)

const (
	lockRetryBound    = 1 * time.Second
	lockRetryInterval = 20 * time.Millisecond
)

// storeAccountKeyPattern matches exactly the shape profile.AccountKey
// produces (64 lowercase hex chars) — the same whitelist-not-blacklist
// discipline profile.go's validateAccountKey uses, so a hand-crafted key
// like "." or "../.." can never reach a filesystem path built from it.
var storeAccountKeyPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

// AccountRef names one account's row in the store: a provider key (as
// provider.Normalize resolves it) plus the profile.AccountKey-shaped account
// key. It is the same (provider, accountKey) pair profile.Store's entry
// points key on, so a caller already holding one for the profile layer can
// reuse it here without re-deriving anything.
type AccountRef struct {
	ProviderKey string
	AccountKey  string
}

// storeKey validates both fields and returns the row's on-disk map key
// ("<provider>:<accountKey>"). ':' can never appear in either validated
// component, so the composite is unambiguous to split back apart if a future
// caller ever needs to.
func (r AccountRef) storeKey() (string, error) {
	p := provider.Normalize(r.ProviderKey)
	if p == "" || p == "." || p == ".." || strings.ContainsAny(p, "/\\:") {
		return "", fmt.Errorf("usage: invalid provider key %q", r.ProviderKey)
	}
	if !storeAccountKeyPattern.MatchString(r.AccountKey) {
		return "", fmt.Errorf("usage: invalid account key %q", r.AccountKey)
	}
	return p + ":" + r.AccountKey, nil
}

// storeFile is the on-disk shape of <stateDir>/usage/usage.json.
type storeFile struct {
	SchemaVersion int                  `json:"schemaVersion"`
	Accounts      map[string]*storeRow `json:"accounts"`
}

// storeRow is one account's persisted fetch/backoff state, mirroring
// usage_store.py's per-slot dict fields (minus the identity-guard fields —
// see this file's package doc). Every timestamp is epoch seconds (matching
// Python's time.time()); a nil pointer is Python's None (field absent),
// EXCEPT ClaimUntil, whose nil-vs-zero distinction is load-bearing — see
// Entry.ClaimUntil's doc.
type storeRow struct {
	LastGood            *provider.Usage `json:"lastGood,omitempty"`
	FetchedAt           *float64        `json:"fetchedAt,omitempty"`
	LastAttemptAt       *float64        `json:"lastAttemptAt,omitempty"`
	ConsecutiveFailures int             `json:"consecutiveFailures,omitempty"`
	LastError           string          `json:"lastError,omitempty"`
	BackoffUntil        *float64        `json:"backoffUntil,omitempty"`
	NextPollAt          *float64        `json:"nextPollAt,omitempty"`
	PollIntervalS       *float64        `json:"pollIntervalS,omitempty"`
	Last429At           *float64        `json:"last429At,omitempty"`
	AuthDeadStrikes     int             `json:"authDeadStrikes,omitempty"`
	ClaimID             string          `json:"claimId,omitempty"`
	// ClaimUntil is nil when never claimed (Python None); a non-nil pointer
	// — INCLUDING one pointing at 0.0 — means a claim was stamped and
	// (possibly) already cleared. Record clears a claim by writing 0.0, NOT
	// by writing nil: mirrors usage_store.py record()'s apply()
	// (`row["claimUntil"] = 0.0`, not None), which matters for liveClaim's
	// legacy fallback — see Entry.ClaimUntil.
	ClaimUntil *float64 `json:"claimUntil,omitempty"`
}

// Entry is the read model for one account, returned by Store.Entries.
type Entry struct {
	// LastGood is the most recent successful measurement, or nil if none has
	// ever landed. Decision code should gate on Fresh/InBackoff/AgeS rather
	// than trusting LastGood's mere presence — it can be arbitrarily stale.
	LastGood *provider.Usage
	// FetchedAt is when LastGood was recorded; the zero Time means never.
	FetchedAt time.Time
	// AgeS is time.Since(FetchedAt) as of the Entries() call that produced
	// this Entry, or nil when FetchedAt is zero (never fetched).
	AgeS *float64
	// LastAttemptAt is the last Claim or Record touch (success or failure);
	// the zero Time means never attempted.
	LastAttemptAt       time.Time
	ConsecutiveFailures int
	LastError           string
	// BackoffUntil is when a failure's backoff lifts; the zero Time means no
	// live backoff (either never failed, or cleared by a later success).
	BackoffUntil time.Time
	// NextPollAt/PollIntervalS are the scheduler's last-persisted plan
	// (Record's Plan field, propagated from a future caller's
	// PlanAfterFetch); zero/nil when no plan has been recorded yet.
	NextPollAt    time.Time
	PollIntervalS *float64
	// Last429At is when a 429 was last observed on this account; the zero
	// Time means never — matches Recent429's own "zero Time means never
	// seen" contract (policy.go), which this Entry's Recent429 method
	// delegates to unchanged.
	Last429At       time.Time
	AuthDeadStrikes int
	// ClaimUntil is nil when this row has never been claimed (Python None);
	// a non-nil pointer means a claim was stamped, INCLUDING one whose
	// *ClaimUntil is in the past (Record's clear, mirroring
	// usage_store.py's 0.0-not-None clear — see storeRow.ClaimUntil). This
	// nil-vs-set distinction, not just "is the time in the future", is what
	// liveClaim needs to route between the fenced check and the legacy
	// lastAttemptAt fallback exactly as _live_claim does.
	ClaimUntil *time.Time
}

// Fresh reports whether LastGood is recent enough to serve without a new
// fetch: fetched within ServeTTLS (policy.go) of now. Mirrors
// usage_store.UsageEntry.fresh's default-TTL case exactly (ServeTTLS IS
// tsamx's SERVE_TTL_S, re-exported from poll_policy.py by policy.go).
func (e Entry) Fresh(now time.Time) bool {
	return !e.FetchedAt.IsZero() && now.Sub(e.FetchedAt) <= secondsToDuration(ServeTTLS)
}

// InBackoff reports whether a failure's backoff is still live. Mirrors
// usage_store.UsageEntry.in_backoff exactly.
func (e Entry) InBackoff(now time.Time) bool {
	return !e.BackoffUntil.IsZero() && now.Before(e.BackoffUntil)
}

// Recent429 reports whether this account 429'd recently enough to keep the
// post-429 cadence engaged. Delegates to the package-level Recent429
// (policy.go), which is already the exact port of
// usage_store.UsageEntry.recent_429 (anchor-on-backoff-end, lastError-guarded
// — see that function's doc) — this method exists only so a caller holding
// an Entry does not have to unpack three fields to call it.
func (e Entry) Recent429(now time.Time) bool {
	return Recent429(e.Last429At, e.BackoffUntil, e.LastError, now)
}

// Claimed reports whether another collector's fetch lease on this row is
// still live. Mirrors usage_store.UsageEntry.claimed / _live_claim exactly
// (see liveClaim).
func (e Entry) Claimed(now time.Time) bool {
	return liveClaim(e.ClaimUntil, e.LastAttemptAt, now)
}

// TokenDead reports whether this account's refresh-token lineage is provably
// dead (AuthDeadStrikes has reached AuthDeadStrikesThreshold). Mirrors
// usage_store.UsageEntry.token_dead exactly.
func (e Entry) TokenDead() bool {
	return e.AuthDeadStrikes >= AuthDeadStrikesThreshold
}

// PlanInput builds a policy.PlanInput (policy.go) from this Entry plus the
// caller's freshly-fetched newUsage, so a caller wiring PlanAfterFetch on
// top of this store does not have to hand-assemble
// PrevIntervalS/PrevUsage/Recent429 itself — this is the concrete connection
// point the P5 task brief asks for ("P4의 PlanAfterFetch/
// NextPollAfterFetchError가 요구하는 입력을 이 저장소가 채워 줄 수 있어야
// 한다"). The caller still supplies isActive/thresholdPct/models: those are
// per-call scheduling inputs this store has no way to infer from stored
// state alone.
func (e Entry) PlanInput(newUsage *provider.Usage, isActive bool, thresholdPct float64, models []string, now time.Time) PlanInput {
	return PlanInput{
		PrevIntervalS: e.PollIntervalS,
		PrevUsage:     e.LastGood,
		NewUsage:      newUsage,
		IsActive:      isActive,
		ThresholdPct:  thresholdPct,
		Models:        models,
		Recent429:     e.Recent429(now),
		Now:           now,
	}
}

// liveClaim is the direct port of usage_store._live_claim (:66-83): the
// fenced claimUntil wins when present (a non-nil pointer, regardless of
// whether the time it names is in the future or past); a row that has never
// been claimed (nil) falls back to lastAttemptAt + legacyClaimTTLS.
func liveClaim(claimUntil *time.Time, lastAttemptAt time.Time, now time.Time) bool {
	if claimUntil != nil {
		return now.Before(*claimUntil)
	}
	return !lastAttemptAt.IsZero() && now.Sub(lastAttemptAt) < secondsToDuration(legacyClaimTTLS)
}

// FetchRecord is the outcome of one fetch attempt, handed to Store.Record.
// Mirrors usage_store.FetchRecord's success/failure shapes (this port omits
// its third, "sentinel", shape — see this file's package doc on scope).
type FetchRecord struct {
	// Usage is the measurement on success (may itself be nil when the
	// response carried no window data — a valid, still-successful outcome).
	Usage *provider.Usage
	// Error is "" on success, else a failure-kind string — either one of
	// collector.go's FetchError.Kind values ("http-429", "timeout", ...) or
	// one of refresh.go's RefreshOutcome.Error values ("invalid_grant",
	// "no_refresh_token", "transient") when the caller folds a failed
	// refresh attempt into this record.
	Error string
	// RetryAfterS is the server's Retry-After in seconds, when the failure
	// carried one (mirrors collector.FetchError.RetryAfterS).
	RetryAfterS *float64
	// Plan is the caller's PlanAfterFetch result for a SUCCESSFUL fetch —
	// nil leaves the row's existing NextPollAt/PollIntervalS untouched. A
	// failure's next-poll time is computed internally by Record via
	// NextPollAfterFetchError (policy.go), matching
	// usage_store.py record()'s own split (the caller supplies the success
	// plan; the store computes the failure backoff itself).
	Plan *Plan
}

// Store is the <stateDir>/usage/usage.json table. All writes go
// read-modify-write under <stateDir>/usage/.usage.lock; reads (Entries) are
// lock-free, matching usage_store.UsageStore's own "reads are lock-free
// (writes are atomic replaces)" contract.
type Store struct {
	path     string
	lockPath string
	clock    func() time.Time
}

// OpenStore ensures <stateDir>/usage exists (0700) and returns a Store over
// it. stateDir is the same AMX_STATE_DIR root profile.Open takes; this
// store's own subdirectory ("usage") never collides with profile.Store's
// ("profiles") or any other sibling under that root.
func OpenStore(stateDir string) (*Store, error) {
	if stateDir == "" {
		return nil, errors.New("usage: empty state dir")
	}
	dir := filepath.Join(stateDir, storeSubdir)
	if err := rejectSymlinkPath(dir); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	return &Store{
		path:     filepath.Join(dir, storeFileName),
		lockPath: filepath.Join(dir, lockFileName),
		clock:    time.Now,
	}, nil
}

// rejectSymlinkPath lstat's path and errors if it exists and is a symlink —
// the same guard profile.go's rejectSymlink applies to every path it
// touches (minor, adversarial review: "usage.json 자리가 심링크면 추종해
// 대상을 덮어쓴다"). A symlink planted at this store's directory, data file,
// or lock file before it is ever used would redirect reads to, and put
// writes/locks at, wherever it points; refusing to proceed at all closes
// that regardless of the exact rename/open semantics on any given platform.
// A path that does not exist yet is fine — the caller's own MkdirAll/
// os.CreateTemp+Rename is what brings it into existence as a plain file.
func rejectSymlinkPath(path string) error {
	info, err := os.Lstat(path)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil
		}
		return err
	}
	if info.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("usage: %s is a symlink, refusing to use it", path)
	}
	return nil
}

// lock acquires the store's single advisory lock, retrying on contention up
// to lockRetryBound — the same bounded-retry shape profile.go's lock() uses
// (a stuck lock file must never hang a caller indefinitely).
func (s *Store) lock() (*fslock.Lock, error) {
	if err := rejectSymlinkPath(s.lockPath); err != nil {
		return nil, err
	}
	deadline := time.Now().Add(lockRetryBound)
	for {
		l, err := fslock.TryLock(s.lockPath)
		if err == nil {
			return l, nil
		}
		if !errors.Is(err, fslock.ErrWouldBlock) || time.Now().After(deadline) {
			return nil, fmt.Errorf("usage: lock %s held: %w", s.lockPath, err)
		}
		time.Sleep(lockRetryInterval)
	}
}

// readRows reads and parses the store file. recovered is true only when the
// file EXISTED but could not be trusted (corrupt JSON, or a schema version
// this code does not recognize) — never true for a simply-absent file, which
// is the ordinary first-run state, not corruption. Mirrors
// usage_store.UsageStore._read_rows's forgiving contract ("legacy snapshot or
// future schema: start empty") but SURFACES the recovery, per the P5 task
// brief ("파싱 실패 시 빈 상태로 시작하되 그 사실을 반환") — tsamx's own
// _read_rows does not report this to its caller at all.
func (s *Store) readRows() (rows map[string]*storeRow, recovered bool, err error) {
	if err := rejectSymlinkPath(s.path); err != nil {
		return nil, false, err
	}
	raw, rerr := os.ReadFile(s.path)
	if rerr != nil {
		if errors.Is(rerr, fs.ErrNotExist) {
			return map[string]*storeRow{}, false, nil
		}
		return nil, false, rerr
	}
	var sf storeFile
	if uerr := json.Unmarshal(raw, &sf); uerr != nil {
		return map[string]*storeRow{}, true, nil
	}
	if sf.SchemaVersion != storeSchemaVersion {
		return map[string]*storeRow{}, true, nil
	}
	if sf.Accounts == nil {
		return map[string]*storeRow{}, false, nil
	}
	return sf.Accounts, false, nil
}

// writeRows atomically replaces the store file (temp file in the same
// directory + rename, 0600) so a concurrent lock-free Entries reader never
// observes a partial write.
func (s *Store) writeRows(rows map[string]*storeRow) error {
	if err := rejectSymlinkPath(s.path); err != nil {
		return err
	}
	b, err := json.Marshal(storeFile{SchemaVersion: storeSchemaVersion, Accounts: rows})
	if err != nil {
		return err
	}
	return atomicWriteStore(s.path, b, 0o600)
}

// Reserve atomically wins the right to fetch: re-checks eligibility UNDER
// THE STORE'S LOCK and stamps a bounded lease (lastAttemptAt=now,
// claimId/claimUntil=now+ClaimTTLS) ONLY on rows that pass, returning the
// fencing ids Record needs to accept each eventual outcome.
//
// REPLACES this file's former Claim (adversarial review A2, reproduced
// empirically): Claim ported usage_store.UsageStore.claim(), but
// `grep -rn "\.claim(\|\.reserve(" tsamx/src/tsamx/*.py` shows claim() is
// NEVER called by any production code path — every real caller
// (switcher.py:3691,3698) uses reserve() instead, whose own docstring
// states plainly what the unconditional claim()-then-check-Entries-yourself
// pattern this file previously documented actually is: "lets two collectors
// both pass the check and both fetch; the re-check under the lock closes
// that window." Deciding eligibility on a lock-free Entries() read and
// leasing separately is exactly that open window; reproduced here as
// TestReserve_ConcurrentCallersOnlyOneWins (two Store handles on the same
// stateDir, raced deliberately — the removed Claim let both win).
//
// Eligibility (ALL must hold, checked against each row's CURRENT on-disk
// state under this call's lock, never a caller's possibly-stale Entries()
// snapshot): !Entry.Claimed (no live lease held by another collector),
// !Entry.InBackoff (no active failure backoff), !Entry.TokenDead (the
// refresh-token lineage is not provably dead — a quarantined account has
// nothing to gain from another fetch), and !Entry.Fresh (the last-good
// measurement is not already recent enough to serve without a new fetch). A
// row that has never been touched (no stored row at all) is always
// eligible — mirrors reserve()'s own "row doesn't match identity -> always
// eligible" branch (usage_store.py: the `if not self._matches(...): ...
// else: if not _row_eligible(...)` split) — there being no prior state IS
// the reason to fetch, not a disqualifying one.
//
// SCOPE (deliberate, not an oversight — see the package doc's "Scope
// boundary" section): tsamx's _row_eligible additionally gates on
// nextPollAt/pollIntervalS ("poll-due", under respect_plans/
// repair_overslept caller modes) — deciding WHICH of several already-
// eligible rows is due RIGHT NOW under an adaptive cadence. That is
// scheduling policy for the P5 AutoSwitch engine this task must not build;
// this port covers only the four EXCLUSIVITY conditions above, which exist
// solely to stop two collectors double-fetching the SAME row.
func (s *Store) Reserve(refs []AccountRef) (won map[AccountRef]string, recovered bool, err error) {
	if len(refs) == 0 {
		return map[AccountRef]string{}, false, nil
	}
	keyed := make(map[AccountRef]string, len(refs))
	for _, r := range refs {
		k, kerr := r.storeKey()
		if kerr != nil {
			return nil, false, kerr
		}
		keyed[r] = k
	}

	l, err := s.lock()
	if err != nil {
		return nil, false, err
	}
	defer l.Unlock()

	rows, recovered, err := s.readRows()
	if err != nil {
		return nil, false, err
	}

	now := s.clock()
	nowF := epochSeconds(now)
	won = make(map[AccountRef]string, len(refs))
	for _, r := range refs {
		k := keyed[r]
		row := rows[k]
		if row != nil {
			e := entryFromRow(row)
			if e.Claimed(now) || e.InBackoff(now) || e.TokenDead() || e.Fresh(now) {
				continue
			}
		}
		id, cerr := newClaimID()
		if cerr != nil {
			return nil, false, cerr
		}
		if row == nil {
			row = &storeRow{}
			rows[k] = row
		}
		row.LastAttemptAt = storeFloatPtr(nowF)
		row.ClaimID = id
		row.ClaimUntil = storeFloatPtr(nowF + ClaimTTLS)
		won[r] = id
	}
	if len(won) > 0 {
		if err := s.writeRows(rows); err != nil {
			return nil, false, err
		}
	}
	return won, recovered, nil
}

// Record merges fetch outcomes into the store, fenced by the claim ids
// Reserve handed out. Mirrors usage_store.UsageStore.record's field-level
// behavior exactly (success resets the failure fields; failure never
// touches LastGood/FetchedAt; a supplied success Plan commits atomically
// with its measurement).
//
// claims fences each outcome: an entry is accepted only when the row's
// current ClaimID equals claims[ref] — a late writer whose lease was
// replaced by a newer Reserve is silently ignored, never overwriting the
// newer row. Passing claims == nil switches to the UNFENCED mode
// usage_store.py's record() also supports: an outcome is accepted unless the
// row carries a claim id AND that claim's stamped ClaimUntil is still in the
// future — checked directly against ClaimUntil (0.0 when unset, per
// storeRow's doc), deliberately NOT via liveClaim's legacy lastAttemptAt
// fallback (mirrors record()'s own comment: "record() deliberately checks
// only the fenced form"). Returns which refs were actually accepted, plus
// whether the read this call performed had to recover from a corrupt/
// foreign-schema file (adversarial review minor: this used to be silently
// discarded, leaving a caller with no way to notice).
func (s *Store) Record(outcomes map[AccountRef]FetchRecord, claims map[AccountRef]string) (accepted map[AccountRef]bool, recovered bool, err error) {
	if len(outcomes) == 0 {
		return map[AccountRef]bool{}, false, nil
	}
	keyed := make(map[AccountRef]string, len(outcomes))
	for r := range outcomes {
		k, kerr := r.storeKey()
		if kerr != nil {
			return nil, false, kerr
		}
		keyed[r] = k
	}

	l, err := s.lock()
	if err != nil {
		return nil, false, err
	}
	defer l.Unlock()

	rows, recovered, err := s.readRows()
	if err != nil {
		return nil, false, err
	}

	now := s.clock()
	accepted = make(map[AccountRef]bool, len(outcomes))
	for r, rec := range outcomes {
		k := keyed[r]
		row := rows[k]
		if claims != nil {
			expected, ok := claims[r]
			if !ok || expected == "" || row == nil || row.ClaimID != expected {
				continue
			}
		} else if row != nil && row.ClaimID != "" {
			cu := 0.0
			if row.ClaimUntil != nil {
				cu = *row.ClaimUntil
			}
			if epochSeconds(now) < cu {
				continue
			}
		}
		if row == nil {
			row = &storeRow{}
			rows[k] = row
		}
		applyRecord(row, rec, now)
		accepted[r] = true
	}
	if len(accepted) > 0 {
		if err := s.writeRows(rows); err != nil {
			return nil, false, err
		}
	}
	return accepted, recovered, nil
}

// applyRecord is Record's per-row merge, mirroring usage_store.py record()'s
// apply() closure field-for-field.
func applyRecord(row *storeRow, rec FetchRecord, now time.Time) {
	row.ClaimID = ""
	row.ClaimUntil = storeFloatPtr(0.0) // cleared-but-set, NOT nil — see storeRow.ClaimUntil
	nowF := epochSeconds(now)
	row.LastAttemptAt = storeFloatPtr(nowF)

	if rec.Error == "" {
		row.LastGood = rec.Usage
		row.FetchedAt = storeFloatPtr(nowF)
		if rec.Plan != nil {
			npa := epochSeconds(rec.Plan.NextPollAt)
			row.NextPollAt = &npa
			iv := rec.Plan.IntervalS
			row.PollIntervalS = &iv
		}
		row.ConsecutiveFailures = 0
		row.LastError = ""
		row.BackoffUntil = nil
		row.AuthDeadStrikes = 0 // a success proves the token is alive
		return
	}

	// Failure: NextPollAfterFetchError (policy.go, P4) is the store's direct
	// connection to the already-ported planner for the failure path — it
	// internally increments the failure streak by one and applies
	// FailureBackoffS, exactly what usage_store.py record()'s failure branch
	// computes inline.
	nextAttempt := NextPollAfterFetchError(now, row.ConsecutiveFailures, &FetchError{
		Kind:        rec.Error,
		RetryAfterS: rec.RetryAfterS,
	})
	row.ConsecutiveFailures++
	row.LastError = rec.Error
	if rec.Error == "http-429" {
		row.Last429At = storeFloatPtr(nowF)
	}
	bu := epochSeconds(nextAttempt)
	row.BackoffUntil = &bu
	if permanentAuthErrors[rec.Error] {
		row.AuthDeadStrikes++
	}
}

// Entries returns the read model for refs (a zero-value Entry for any ref
// with no stored row yet). recovered reports whether the underlying file had
// to be treated as corrupt/foreign-schema on this read — see readRows.
// Lock-free, matching usage_store.UsageStore.entries.
func (s *Store) Entries(refs []AccountRef) (entries map[AccountRef]Entry, recovered bool, err error) {
	entries = make(map[AccountRef]Entry, len(refs))
	if len(refs) == 0 {
		return entries, false, nil
	}
	keyed := make(map[AccountRef]string, len(refs))
	for _, r := range refs {
		k, err := r.storeKey()
		if err != nil {
			return nil, false, err
		}
		keyed[r] = k
	}
	rows, recovered, err := s.readRows()
	if err != nil {
		return nil, false, err
	}
	now := s.clock()
	for _, r := range refs {
		e := entryFromRow(rows[keyed[r]])
		if !e.FetchedAt.IsZero() {
			age := now.Sub(e.FetchedAt).Seconds()
			e.AgeS = &age
		}
		entries[r] = e
	}
	return entries, recovered, nil
}

func entryFromRow(row *storeRow) Entry {
	if row == nil {
		return Entry{}
	}
	return Entry{
		LastGood:            row.LastGood,
		FetchedAt:           timeFromPtrFloat(row.FetchedAt),
		LastAttemptAt:       timeFromPtrFloat(row.LastAttemptAt),
		ConsecutiveFailures: row.ConsecutiveFailures,
		LastError:           row.LastError,
		BackoffUntil:        timeFromPtrFloat(row.BackoffUntil),
		NextPollAt:          timeFromPtrFloat(row.NextPollAt),
		PollIntervalS:       row.PollIntervalS,
		Last429At:           timeFromPtrFloat(row.Last429At),
		AuthDeadStrikes:     row.AuthDeadStrikes,
		ClaimUntil:          claimUntilFromRow(row.ClaimUntil),
	}
}

// ClearDeadToken resets the quarantine strikes and failure/backoff state for
// refs whose credential was just refreshed or re-staged (e.g. via
// StageRefreshedCredential or a real re-login) — mirrors
// usage_store.UsageStore.clear_dead_token exactly. A no-op (not an error)
// for a ref with no stored row. This resets counters on THIS state row only;
// it is not, and must not be confused with, the P5 AutoSwitch engine's
// separate quarantine STATE FILE (design note P5's "격리 상태 파일 원자
// 쓰기") — that file does not exist yet and this function does not create or
// touch anything resembling it.
func (s *Store) ClearDeadToken(refs []AccountRef) error {
	if len(refs) == 0 {
		return nil
	}
	keyed := make(map[AccountRef]string, len(refs))
	for _, r := range refs {
		k, err := r.storeKey()
		if err != nil {
			return err
		}
		keyed[r] = k
	}

	l, err := s.lock()
	if err != nil {
		return err
	}
	defer l.Unlock()

	rows, _, err := s.readRows()
	if err != nil {
		return err
	}
	changed := false
	for _, r := range refs {
		row := rows[keyed[r]]
		if row == nil {
			continue
		}
		row.ClaimID = ""
		row.ClaimUntil = storeFloatPtr(0.0)
		row.AuthDeadStrikes = 0
		row.ConsecutiveFailures = 0
		row.LastError = ""
		row.BackoffUntil = nil
		changed = true
	}
	if !changed {
		return nil
	}
	return s.writeRows(rows)
}

// -- small helpers -----------------------------------------------------

func newClaimID() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", fmt.Errorf("usage: generate claim id: %w", err)
	}
	return hex.EncodeToString(b[:]), nil
}

func epochSeconds(t time.Time) float64 {
	return float64(t.UnixNano()) / 1e9
}

func timeFromEpochSeconds(f float64) time.Time {
	sec := int64(f)
	nsec := int64((f - float64(sec)) * 1e9)
	return time.Unix(sec, nsec).UTC()
}

func timeFromPtrFloat(f *float64) time.Time {
	if f == nil {
		return time.Time{}
	}
	return timeFromEpochSeconds(*f)
}

// claimUntilFromRow preserves the nil-vs-set distinction storeRow.ClaimUntil
// documents: a nil input stays nil (never claimed); a non-nil input
// (including 0.0) becomes a non-nil *time.Time.
func claimUntilFromRow(f *float64) *time.Time {
	if f == nil {
		return nil
	}
	t := timeFromEpochSeconds(*f)
	return &t
}

// floatPtr returns a pointer to a fresh copy of v — safe to call once per
// row even inside a loop, unlike taking the address of a reused loop
// variable.
func storeFloatPtr(v float64) *float64 { return &v }

// atomicWriteStore writes data to path via a temp file in the same directory
// + rename, so a concurrent lock-free Entries reader never observes a
// partial write. Duplicated (not shared) from profile.go's atomicWrite and
// internal/store's own copy — same small-and-private convention that file's
// own doc comment states for why each package keeps its own.
func atomicWriteStore(path string, data []byte, perm os.FileMode) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".amx-usage-*.tmp")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer func() {
		if tmpName != "" {
			_ = os.Remove(tmpName)
		}
	}()
	if err := tmp.Chmod(perm); err != nil {
		_ = tmp.Close()
		return err
	}
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmpName, path); err != nil {
		return err
	}
	tmpName = ""
	return nil
}
