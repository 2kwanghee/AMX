// Package tsamx bridges the AMA daemon to the tsamx CLI (design note §6, SSOT
// §6.3). All effects go through the Bridge interface so tests use a fake and
// never exec the real CLI. The exec implementation runs against the agent's own
// CLAUDE_CONFIG_DIR / XDG_DATA_HOME and parses `--json` output.
package tsamx

import "context"

// AddRequest carries everything `tsamx add` needs for one account. CredentialJSON
// is the plaintext OAuth credential set — it MUST NEVER be logged (§7).
//
// `tsamx add` takes no account argument: it captures whichever account the
// Claude config home currently holds. AccountUUID and OrganizationName are the
// identity tsamx reads out of that home's `.claude.json`, so the bridge has to
// stage them alongside the credential before invoking the verb.
type AddRequest struct {
	Email            string
	AccountUUID      string
	OrganizationName string
	CredentialJSON   []byte
	ConfigDir        string // overrides the bridge's CLAUDE_CONFIG_DIR for this call
	DataHome         string // overrides the bridge's XDG_DATA_HOME for this call
	Enable           bool   // true -> leave enabled (rotation candidate); false -> disable
}

// Bridge is the tsamx control surface AMA depends on. Every method is expected
// to be idempotent at the CLI level (adding an existing account, removing an
// absent one, etc. are no-ops that succeed).
type Bridge interface {
	Add(ctx context.Context, req AddRequest) error
	Remove(ctx context.Context, account string) error
	Enable(ctx context.Context, account string) error
	Disable(ctx context.Context, account string) error
	Switch(ctx context.Context, target string) error
	List(ctx context.Context) (*ListResult, error)
	Status(ctx context.Context) (*StatusResult, error)

	// AutoOnce runs `tsamx auto --once` (a single evaluate-and-maybe-switch
	// tick) and returns its exit code: 0 switched, 2 no action, 3 blocked (no
	// viable target / all exhausted). A non-nil error is returned only for a
	// real failure (exit 1, or the process could not run); codes 0/2/3 are
	// normal outcomes returned with a nil error (design note §2, §6).
	AutoOnce(ctx context.Context) (int, error)
	// SwitchStrategy runs `tsamx switch --strategy <strategy>` where strategy is
	// "best" or "next-available" (design note §3).
	SwitchStrategy(ctx context.Context, strategy string) error
	// ConfigSetThreshold runs `tsamx config set autoswitch.threshold <pct>`
	// (O4-C SetPolicy delivery, design note §O4-C).
	ConfigSetThreshold(ctx context.Context, pct float64) error
	// AutoStatePath returns the path to tsamx's autoswitch_state.json, watched
	// for quarantine changes (design note §2 dual detection). Empty when the
	// path cannot be resolved, in which case the watcher is not started.
	AutoStatePath() string
	// ReadQuarantine returns the currently quarantined accounts as number->email
	// from autoswitch_state.json. A missing/unreadable state file yields an empty
	// map and a nil error (nothing quarantined).
	ReadQuarantine(ctx context.Context) (map[string]string, error)

	// DeliverLock takes an exclusive, cross-process advisory lock over the
	// runner's config home for the whole deliver credential-swap critical section
	// (write -> add -> restore, SSOT §6.3 / B1b) and returns a release func. A
	// runner launched through the deploy/amx-claude wrapper takes a *shared* lock
	// on the same file before it reads .credentials.json, so it can never start
	// up inside the window where the freshly delivered account is momentarily
	// active — closing the over-charge window that B1a only narrows. This is
	// distinct from the engine lock (which serializes AMA-internal mutations); the
	// flock coordinates AMA with the separate runner processes. The lock is
	// process-associated, so an AMA crash releases it automatically. Bridges with
	// no config home return a no-op release and a nil error.
	DeliverLock(ctx context.Context) (func() error, error)
}

// ListResult mirrors `tsamx list --json` (tsamx json_output schema v1).
type ListResult struct {
	SchemaVersion       int          `json:"schemaVersion"`
	ActiveAccountNumber *int         `json:"activeAccountNumber"`
	Accounts            []AccountRow `json:"accounts"`
}

// AccountRow is one row of `tsamx list --json`. Only the fields AMA consumes are
// modeled; unknown fields are ignored.
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

// Usage is the per-account usage projection (tsamx usage_to_json).
type Usage struct {
	FiveHour *Window `json:"fiveHour"`
	SevenDay *Window `json:"sevenDay"`
}

// Window is one binding window's utilization.
type Window struct {
	Pct      float64 `json:"pct"`
	ResetsAt string  `json:"resetsAt"`
}

// StatusResult is a minimal projection of `tsamx status --json`: which account
// currently holds the live credential.
type StatusResult struct {
	ActiveAccountNumber *int   `json:"activeAccountNumber"`
	ActiveEmail         string `json:"activeEmail"`
}
