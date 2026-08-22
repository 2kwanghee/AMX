package autoswitch

import "time"

// EventSchemaVersion mirrors tsamx.json_output.SCHEMA_VERSION as carried on
// every AutoSwitchEvent.to_json() payload (autoswitch.py:267-273) — kept
// equal so a consumer already parsing tsamx JSONL auto-switch events needs
// no version branch for this engine's events.
const EventSchemaVersion = 1

// AccountRef is the {"number":N,"email":"..."} shape tsamx's _ref() helper
// builds (autoswitch.py:593-594) and every event's from/to/active field
// carries.
type AccountRef struct {
	Number int    `json:"number"`
	Email  string `json:"email"`
}

// Event is implemented by every event type below. ToJSON's payload shape —
// schemaVersion/event/ts plus the type's own fields — matches
// AutoSwitchEvent.to_json() (autoswitch.py:267-273): additive, so a future
// field never breaks an existing consumer (contract C8's "미지 필드 무시"
// principle applied to this engine's own output, not just tsamx's).
//
// Only the four kinds the task named explicitly (autoswitch.py:257-397) plus
// the two this package's Decide loop needs to represent quarantine
// release and full exhaustion (UnquarantineEvent, AllExhaustedEvent — both
// also present in the original, autoswitch.py:400-425) are ported. Sleep/
// Error/ConfigWarning are loop/runtime concerns the polling loop (out of
// scope here) would emit, not the pure decision.
type Event interface {
	Kind() string
	ToJSON() map[string]any
}

func baseJSON(kind string, ts time.Time) map[string]any {
	return map[string]any{
		"schemaVersion": EventSchemaVersion,
		"event":         kind,
		"ts":            ts.UTC().Format(time.RFC3339),
	}
}

// PollEvent ports autoswitch.py:280-336 (PollEvent), restricted to the
// fields this package's Decide computes (fetchErrors/windowsPct are the
// polling collector's concern, not this decision core's — omitted here
// rather than faked).
type PollEvent struct {
	Ts           time.Time
	Active       *AccountRef
	HeadroomPct  map[string]*float64 // account number (string) -> headroom pct, nil = unknown
	ThresholdPct float64
}

func (e PollEvent) Kind() string { return "poll" }
func (e PollEvent) ToJSON() map[string]any {
	m := baseJSON(e.Kind(), e.Ts)
	m["active"] = e.Active
	m["headroomPct"] = e.HeadroomPct
	m["threshold"] = e.ThresholdPct
	return m
}

// SwitchEvent ports autoswitch.py:339-366 (SwitchEvent). Trigger is
// "proactive" | "at-limit" (the two triggers this package's threshold gate
// produces — "failover"/"consume-first" are out of scope, see package doc).
//
// Server mapping: the scheduler maps a code-0 AutoOnce result to
// amxv1.AccountEvent_KIND_SWITCH (internal/scheduler/scheduler.go:202-221) —
// a SwitchEvent here is that same moment, one layer down from the exit code.
type SwitchEvent struct {
	Ts      time.Time
	Trigger string
	From    *AccountRef
	To      *AccountRef
}

func (e SwitchEvent) Kind() string { return "switch" }
func (e SwitchEvent) ToJSON() map[string]any {
	m := baseJSON(e.Kind(), e.Ts)
	m["trigger"] = e.Trigger
	m["from"] = e.From
	m["to"] = e.To
	return m
}

// NoSwitchEvent ports autoswitch.py:369-380 (NoSwitchEvent). Reason is one
// of: no-active-account, active-usage-unknown, below-threshold, cooldown,
// no-candidates, no-comparison, no-qualifying-candidate,
// candidates-exhausted, no-valid-target (see decide.go for which trigger
// each comes from).
type NoSwitchEvent struct {
	Ts     time.Time
	Reason string
	Detail string
}

func (e NoSwitchEvent) Kind() string { return "no-switch" }
func (e NoSwitchEvent) ToJSON() map[string]any {
	m := baseJSON(e.Kind(), e.Ts)
	m["reason"] = e.Reason
	m["detail"] = e.Detail
	return m
}

// QuarantineEvent ports autoswitch.py:382-397 (QuarantineEvent).
//
// Server mapping: the scheduler's fsnotify watch on the quarantine state
// file enqueues amxv1.AccountEvent_KIND_QUARANTINE on a newly-appearing slot
// (internal/scheduler/scheduler.go:291-303) — this event is emitted at the
// moment WriteState persists that same new entry (see quarantine.go), one
// layer above the file-watch AMA's tsamx path uses today.
type QuarantineEvent struct {
	Ts     time.Time
	Number string
	Email  string
	Reason string
}

func (e QuarantineEvent) Kind() string { return "account-quarantined" }
func (e QuarantineEvent) ToJSON() map[string]any {
	m := baseJSON(e.Kind(), e.Ts)
	m["number"] = e.Number
	m["email"] = e.Email
	m["reason"] = e.Reason
	return m
}

// UnquarantineEvent ports autoswitch.py:400-411 (UnquarantineEvent) — a
// quarantined slot's status recovered (see ShouldRelease).
type UnquarantineEvent struct {
	Ts     time.Time
	Number string
	Email  string
	Reason string
}

func (e UnquarantineEvent) Kind() string { return "account-unquarantined" }
func (e UnquarantineEvent) ToJSON() map[string]any {
	m := baseJSON(e.Kind(), e.Ts)
	m["number"] = e.Number
	m["email"] = e.Email
	m["reason"] = e.Reason
	return m
}

// AllExhaustedEvent ports autoswitch.py:414-425 (AllExhaustedEvent) — every
// oauth candidate (and the active account) measured and at its limit.
//
// Server mapping: exit code 3 maps to amxv1.AccountEvent_KIND_ALL_EXHAUSTED
// (internal/scheduler/scheduler.go:225-236) — this event is the decision
// core's own record of the same verdict CodeBlocked(reason="all-exhausted")
// reports as an exit code.
type AllExhaustedEvent struct {
	Ts              time.Time
	EarliestResetAt *time.Time
}

func (e AllExhaustedEvent) Kind() string { return "all-exhausted" }
func (e AllExhaustedEvent) ToJSON() map[string]any {
	m := baseJSON(e.Kind(), e.Ts)
	if e.EarliestResetAt != nil {
		m["earliestResetAt"] = e.EarliestResetAt.UTC().Format(time.RFC3339)
	} else {
		m["earliestResetAt"] = nil
	}
	return m
}
