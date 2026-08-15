package provider

import "context"

// DefaultProvider is the provider key an empty AccountRef.provider normalizes to.
// The wire contract (proto AccountRef field 4) defines an empty provider as
// "claude", so both the command router and the manifest read an empty value as
// this key. It is the one provider name the vendor-neutral layer knows by name,
// solely to honor that wire default.
const DefaultProvider = "claude"

// Normalize maps an empty provider key to DefaultProvider and returns any other
// key unchanged. It is the single place the empty="claude" wire rule is applied,
// so the router and the store agree on which registry entry a command targets.
func Normalize(providerKey string) string {
	if providerKey == "" {
		return DefaultProvider
	}
	return providerKey
}

// AddRequest carries everything a pool `add` needs for one account.
// CredentialJSON is the plaintext OAuth credential set — it MUST NEVER be logged
// (§7).
//
// The pool `add` verb takes no account argument: it captures whichever account
// the config home currently holds. AccountUUID and OrganizationName are the
// identity the driver stages into that home before the verb runs, so the bridge
// passes them through to Driver.StageCredential as neutral AddMeta.
type AddRequest struct {
	Email            string
	AccountUUID      string
	OrganizationName string
	CredentialJSON   []byte
	ConfigDir        string // overrides the bridge's provider config home for this call
	DataHome         string // overrides the bridge's pool data root for this call
	Enable           bool   // true -> leave enabled (rotation candidate); false -> disable
}

// Bridge is the pool control surface AMA depends on for one provider. Every
// method is expected to be idempotent at the CLI level (adding an existing
// account, removing an absent one, etc. are no-ops that succeed).
//
// The interface is vendor-neutral: one Bridge serves exactly one provider, and
// the command router selects the Bridge for a command from AccountRef.provider.
// An implementation backs it with that provider's pool CLI; tests use a fake.
type Bridge interface {
	Add(ctx context.Context, req AddRequest) error
	Remove(ctx context.Context, account string) error
	Enable(ctx context.Context, account string) error
	Disable(ctx context.Context, account string) error
	Switch(ctx context.Context, target string) error
	List(ctx context.Context) (*ListResult, error)
	Status(ctx context.Context) (*StatusResult, error)

	// AutoOnce runs a single evaluate-and-maybe-switch tick and returns its exit
	// code: 0 switched, 2 no action, 3 blocked (no viable target / all
	// exhausted). A non-nil error is returned only for a real failure (exit 1, or
	// the process could not run); codes 0/2/3 are normal outcomes returned with a
	// nil error (design note §2, §6).
	AutoOnce(ctx context.Context) (int, error)
	// SwitchStrategy runs a strategy-driven switch where strategy is "best" or
	// "next-available" (design note §3).
	SwitchStrategy(ctx context.Context, strategy string) error
	// ConfigSetThreshold sets autoswitch.threshold (O4-C SetPolicy delivery,
	// design note §O4-C).
	ConfigSetThreshold(ctx context.Context, pct float64) error
	// ConfigSet sets autoswitch.<key> for the rest of the central policy (F4,
	// O4-B): cooldown_seconds and hysteresis_pct. The caller passes only
	// non-negative values (a negative SetPolicy field means "unset" and is skipped
	// upstream).
	ConfigSet(ctx context.Context, key string, value float64) error
	// AutoStatePath returns the path to the pool's autoswitch_state.json, watched
	// for quarantine changes (design note §2 dual detection). Empty when the path
	// cannot be resolved, in which case the watcher is not started.
	AutoStatePath() string
	// ReadQuarantine returns the currently quarantined accounts as number->email
	// from autoswitch_state.json. A missing/unreadable state file yields an empty
	// map and a nil error (nothing quarantined).
	ReadQuarantine(ctx context.Context) (map[string]string, error)

	// DeliverLock takes an exclusive, cross-process advisory lock over the
	// runner's config home for the deliver credential-swap critical section (SSOT
	// §6.3 / B1b) and returns a release func — ALWAYS non-nil, so the caller can
	// unconditionally defer it. A runner launched through the deploy wrapper takes
	// a *shared* lock on the same file before it starts, so it cannot begin inside
	// the window where the freshly delivered account is momentarily active —
	// closing the over-charge window that B1a only narrows.
	//
	// Acquisition is NON-BLOCKING with a bounded retry and is invoked OUTSIDE the
	// engine lock: if it cannot be taken within the bound (e.g. a long-lived runner
	// holds the shared lock) it returns a no-op release and the deliver proceeds
	// WITHOUT the lock (fail-open), so it can never stall the engine lock or the
	// scheduler. Distinct from the engine lock (which serializes AMA-internal
	// mutations); the flock coordinates AMA with the separate runner processes and
	// is process-associated, so an AMA crash releases it automatically.
	DeliverLock(ctx context.Context) func() error
}

// ListResult mirrors the pool `list --json` payload (json_output schema v1).
type ListResult struct {
	SchemaVersion       int          `json:"schemaVersion"`
	ActiveAccountNumber *int         `json:"activeAccountNumber"`
	Accounts            []AccountRow `json:"accounts"`
}

// AccountRow is one row of the pool `list --json`. Only the fields AMA consumes
// are modeled; unknown fields are ignored.
type AccountRow struct {
	Number           int    `json:"number"`
	Email            string `json:"email"`
	OrganizationName string `json:"organizationName"`
	OrganizationUUID string `json:"organizationUuid"`
	Active           bool   `json:"active"`
	Disabled         bool   `json:"disabled"`
	UsageStatus      string `json:"usageStatus"`
	Usage            *Usage `json:"usage"`
	Alias            string `json:"alias"`
}

// Usage is the per-account usage projection (pool usage_to_json).
//
// FiveHour/SevenDay are the original Claude-shaped two-window view, kept for the
// existing reporter consumer. Windows is the vendor-neutral list a driver fills
// with whatever binding windows it actually has (Claude: five_hour/seven_day;
// Codex: primary/secondary): each Window carries its own Id and WindowMinutes so
// no vendor has to force its windows into the fixed two-field shape. Drivers that
// populate both keep them consistent (dual record) until the reporter migrates.
type Usage struct {
	FiveHour *Window  `json:"fiveHour"`
	SevenDay *Window  `json:"sevenDay"`
	Windows  []Window `json:"windows,omitempty"`
	// Spend is the pay-as-you-go spend (tsamx `spend`), carried through to the
	// report untouched. It is informational and never enters the switch/pool math.
	Spend *Spend `json:"spend,omitempty"`
	// Scoped is the per-model weekly windows (tsamx `scoped`). Kept out of Windows
	// so it can never influence maxWindowPct / eligible / allExhausted; each entry
	// names its model in ScopedWindow.Name (tsamx camelCase key `name`).
	Scoped []ScopedWindow `json:"scoped,omitempty"`
}

// Spend mirrors the tsamx `spend` object (pool usage_to_json): pay-as-you-go
// consumption against a monthly cap. Only the fields AMA forwards are modeled;
// countdown/clock and any other keys are ignored.
type Spend struct {
	Used     float64 `json:"used"`
	Limit    float64 `json:"limit"`
	Pct      float64 `json:"pct"`
	Currency string  `json:"currency"`
	ResetsAt string  `json:"resetsAt,omitempty"`
}

// ScopedWindow mirrors one entry of the tsamx `scoped` array: a per-model weekly
// window. Name is the model display name (tsamx key `name`). Pace/countdown keys
// are ignored — only the model, pct, and reset are forwarded.
type ScopedWindow struct {
	Name     string  `json:"name"`
	Pct      float64 `json:"pct"`
	ResetsAt string  `json:"resetsAt,omitempty"`
}

// Window is one binding window's utilization. Id and WindowMinutes are set only
// on entries carried in Usage.Windows (the neutral list); the legacy FiveHour/
// SevenDay fields leave them zero-valued.
type Window struct {
	Id            string  `json:"id,omitempty"`
	WindowMinutes int     `json:"windowMinutes,omitempty"`
	Pct           float64 `json:"pct"`
	ResetsAt      string  `json:"resetsAt"`
}

// StatusResult is a minimal projection of the pool `status`: which account
// currently holds the live credential.
type StatusResult struct {
	ActiveAccountNumber *int   `json:"activeAccountNumber"`
	ActiveEmail         string `json:"activeEmail"`
}
