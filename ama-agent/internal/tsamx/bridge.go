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
