// Package autoswitch implements the P5b decision core of the seat engine's
// auto-switch judgement (design note docs/design-notes/seat-engine-plan.md,
// P5 절, §0 불변 원칙). It is DELIBERATELY INERT, same convention as
// internal/seat/usage (P4): nothing in cmd/ama, the existing tsamx bridge, or
// any scheduler wires anything from this package. The tsamx path stays the
// default and its behavior is unchanged by this package's existence.
//
// # Scope
//
// This package ports ONLY the decision logic of tsamx's
// tsamx/src/tsamx/autoswitch.py — the threshold/hysteresis/cooldown gate,
// the "every account above threshold" recovery escape (_recovery_is_useful),
// quarantine judgement, and the event/exit-code shapes. It does NOT poll,
// does NOT call any HTTP endpoint, does NOT refresh OAuth tokens, and does
// NOT own a usage store. Every input Decide needs — account usage snapshots,
// policy values, the current time, the last switch time, and the current
// quarantine set — is passed in by the caller. That caller-owned half (OAuth
// refresh, the usage state store/lease — internal/seat/usage/refresh.go and
// store.go) was a separate P5 track (branch feat/seat-engine-p5) at the time
// this package started; it has since merged into main (PR #159), so it is
// live code today, not a future dependency — see the "합류 설계" section
// below for the join this package still owes it (NOT done in this commit).
//
// # What was ported vs. deliberately narrowed
//
// # Corrected in review (round 2)
//
// The FIRST version of this package wrongly reused contract C1's manual
// `switch --strategy best|next-available` literals as the tick-strategy
// vocabulary, and had no unhealthy-ticks failover. Both were review
// findings (C2 critical, C3 major) fixed in this revision:
//
//   - Strategy is now StrategyBest | StrategyConsumeFirst, matching tsamx's
//     REAL tick-strategy field (AutoSwitchSettings.strategy, settings.py:
//     47-50, `"best"` or `"consume-first"`). StrategyConsumeFirst ports
//     autoswitch.py's soonest-weekly-reset ranking (autoswitch.py:1846-1856,
//     1888-1891, 1201-1230's distinct NoAction-shaped "nothing to do"
//     reasons). StrategyNextAvailable is still a named constant (contract
//     C1's manual-switch literal) but Decide REJECTS it explicitly
//     (ErrNextAvailableIsManualOnly) rather than reinterpreting it — see
//     Strategy's and Decide's doc.
//   - Policy.UnhealthyTicks + Input.ConsecutiveUnhealthyTicks/
//     Decision.UnhealthyTicks port autoswitch.py:999-1011's failover
//     counter: an active account whose usage stays unreadable for
//     UnhealthyTicks consecutive calls now escapes to trigger "failover"
//     instead of returning NoAction/exit-2 forever (the exact "pool freezes
//     silently" failure mode 08-22's incident report describes for a
//     different mechanism — see AMX project memory
//     amx-pool-blind-swap-incident.md).
//
// tsamx's real tick() (autoswitch.py:843-1266) ALSO implements a
// live-session skip, an API-key candidate fallback, and a "no-return
// account" bar keyed on cross-tick state (lastSwitchFrom/leftHeadroom/
// leftRecoveryAt) that this package still does not port — those remain out
// of scope: the task instructing this port scoped it to "판정 로직만",
// inputs "전부 인자로" (no persisted per-tick memory beyond what the caller
// already tracks: lastSwitchAt, the quarantine set, and now the
// unhealthy-ticks counter), and none of the three is load-bearing for the
// anti-flap/quarantine/exit-code behavior the task asked to pin.
//
// # Quarantine
//
// tsamx quarantines a candidate lazily, only when it actually attempts to
// freshen (refresh) that candidate's token and the refresh answers
// invalid_grant (autoswitch.py:1310-1312, via _freshen_target:749-798). This
// package has no refresh step, so it instead judges quarantine directly off
// the P4 status literal already computed for it
// (internal/seat/usage/expiry.go's StatusReloginRequired vs
// StatusTokenExpired — see ShouldQuarantine's doc for why the distinction
// matters and why conflating them was the exact defect P4's review caught).
//
// # State file
//
// WriteState/ReadState persist quarantine at a path this package owns
// exclusively (StatePath) — never tsamx's autoswitch_state.json. See
// StatePath's doc for why sharing that file would be unsafe.
//
// # 합류 설계 (review C4, corrected review N5) — NOT implemented in this commit
//
// STATUS CORRECTION (review N5): an earlier revision of this comment said
// branch feat/seat-engine-p5 (internal/seat/usage/store.go, refresh.go) was
// "not merged into this branch" — true only in the narrow sense that THIS
// package's branch (feat/seat-engine-p5b) forked from main before that
// merge landed, so store.go/refresh.go are not present in this branch's own
// tree. It is stale as a statement about the PROJECT: feat/seat-engine-p5
// merged into main via PR #159 (commit 38e5d0e) before this revision was
// written. The account-health ledger described below is real, live code on
// main today, not a future/hypothetical track.
//
// internal/seat/usage/store.go already implements its own account-health
// ledger: Store.Entry.TokenDead() (AuthDeadStrikes >=
// AuthDeadStrikesThreshold, store.go:118-124,285-289) and
// Store.ClearDeadToken (store.go:667-677). This package's QuarantineEntry
// map is a SEPARATE ledger, still unreconciled with it — that reconciliation
// is NOT done in this commit (scope discipline: this round's task was N1-N5
// only), but it is a REAL PREREQUISITE, not a someday-nicety:
//
// **MUST be resolved before P6 (섀도 운전) starts.** P6 runs this engine's
// decisions against live pools; two quarantine judgments that can disagree
// with no rule for which wins is exactly the kind of divergence P6's shadow
// comparison exists to catch, and shipping P6 without resolving it first
// guarantees noisy, uninterpretable shadow diffs from day one — the
// discrepancy would be structural, not a bug in either side.
//
// Recorded here so whoever picks this up doesn't have to rediscover the
// shape:
//
//   - Key alignment (DONE this commit): QuarantineEntry/Input.Quarantine/
//     Input.Fingerprints are now keyed by profile.AccountKey(email), the
//     SAME root identifier internal/seat/usage.AccountRef.AccountKey uses
//     (store.go:157-178's storeKey composes "<provider>:<accountKey>" —
//     this package omits the provider prefix because, like
//     internal/seat.Switcher, an instance of this engine is expected to
//     operate within one provider's scope at a time; joining the two only
//     needs the accountKey half compared, or a provider prefix added here
//     if that assumption ever changes).
//   - Single source of truth — candidates and the judgment call, NOT
//     decided here:
//     (a) usage.Store.TokenDead as authoritative: fed by REAL refresh
//     attempts (AuthDeadStrikes increments only on an actual invalid_grant/
//     no_refresh_token answer, store.go:118-124), so it is precise but
//     requires the OAuth-refresh track to actually run against a candidate
//     before it can know anything — an account this engine has never tried
//     to freshen reads as healthy by omission, not "unknown".
//     (b) this package's ShouldQuarantine as authoritative: fed by the P4
//     status literal alone (StatusReloginRequired), available immediately
//     without any refresh attempt, but coarser — it trusts whatever
//     upstream usage collection already classified, rather than proving
//     the lineage dead itself.
//     Decision criterion: if by the time this is picked up the OAuth-
//     refresh track (refresh.go) is routinely invoked against every
//     candidate every cycle (not just the active account), prefer (a) —
//     precision then costs nothing extra. If refresh is still
//     active-account-only, (b) stays load-bearing for candidates refresh
//     never touches, and the join should be "this package quarantines off
//     (b) UNLESS usage.Store.TokenDead answers first, in which case its
//     verdict wins" — i.e. (a) as an override, not a replacement, until
//     refresh coverage is universal.
//   - Release unification (NOT decided here): usage.Store.ClearDeadToken
//     resets ITS OWN AuthDeadStrikes counter when a credential is
//     refreshed/re-staged; this package's ShouldRelease independently
//     judges the SAME real-world event (a replaced credential) off
//     email/fingerprint. A future join should likely have ONE of the two
//     call the other (e.g. Decide's release sweep calling
//     usage.Store.ClearDeadToken when it releases a quarantine, or vice
//     versa) rather than two call sites independently detecting the same
//     credential replacement.
//   - Execution identifiers (DONE this commit): Decision.From/To are now
//     *Target{Number,Email,AccountKey}, so a caller can pass To.AccountKey
//     directly as internal/seat.Switcher.Switch's targetKey without
//     re-deriving anything.
package autoswitch
