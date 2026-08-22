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
// refresh, the usage state store/lease) is a separate P5 track
// (branch feat/seat-engine-p5); the two are meant to converge later, per the
// design note's P5 bullets on "OAuth 토큰 갱신 이식(선결)" and "사용량 상태
// 저장소(선결)".
//
// # What was ported vs. deliberately narrowed
//
// tsamx's real tick() (autoswitch.py:843-1266) also implements a
// "consume-first" strategy, an unhealthy-ticks failover counter, live-session
// skip, API-key candidate fallback, and a "no-return account" bar keyed on
// cross-tick state (lastSwitchFrom/leftHeadroom/leftRecoveryAt). None of
// those are in this package: the task instructing this port scoped it to
// "판정 로직만", inputs "전부 인자로" (no persisted per-tick memory beyond
// what the caller already tracks: lastSwitchAt and the quarantine set), and
// two selection strategies literally named after contract C1's
// `switch --strategy best|next-available` rather than tsamx's tick triggers.
// Decide therefore reinterprets "best"/"next-available" as the STRATEGY the
// threshold/hysteresis/cooldown-gated engine uses to pick a target once a
// switch is warranted — best ports the proactive ranking in
// autoswitch.py:1773-1928 (hysteresis + the all-above recovery escape);
// next-available ports switcher.py's rotate-skip-exhausted CLI strategy
// (switcher.py:4617-4722) as the alternative ranking, since tsamx's tick()
// itself never offers a rotation-style auto-switch strategy. This is P5b's
// own synthesis, not a line-for-line port — flagged here rather than left
// implicit, per the project's code-truth-verification discipline.
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
package autoswitch
