package codex

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/fslock"
	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
)

// Bridge implements provider.Bridge for Codex by manipulating files under a
// single CODEX_HOME. Codex ships no management CLI: one config home holds exactly
// one account as auth.json, so the pool concepts collapse to file operations —
// "disable" logs the account out by renaming auth.json aside, "switch" is a
// no-op (nothing to switch between), and autoswitch has no state to read.
//
// Every method is idempotent: acting on the desired state already in place is a
// no-op that succeeds. Credential material (auth.json contents, tokens) is NEVER
// placed in a log or error string (§7).
var _ provider.Bridge = (*Bridge)(nil)

// disabledSuffix marks auth.json set aside by Disable. Renaming (not deleting)
// preserves the credential so Enable can restore it without a re-login.
const disabledSuffix = ".amx-disabled"

// metaFile is the bridge's sidecar for identity Codex does not store in plaintext
// (auth.json carries no email). List/Status read it to name the account.
const metaFile = ".amx-codex-meta.json"

// deliverLockName is the file flock'd for the deliver critical section (B1b),
// kept separate from the credential files so the runner never touches it.
const deliverLockName = ".amx-deliver.lock"

const (
	deliverLockMaxWait       = 5 * time.Second
	deliverLockRetryInterval = 50 * time.Millisecond
)

// usageTailBytes bounds how much of a rollout jsonl the Usage scan reads from the
// end: only the last token_count event is needed and these files grow large, so
// the whole file is never loaded.
const usageTailBytes = 512 * 1024

// exit codes mirror provider.Bridge.AutoOnce: 0 switched, 2 no action, 3 blocked.
const autoOnceNoAction = 2

// Bridge is the Codex file-manipulation control surface for one config home.
type Bridge struct {
	// Driver owns the vendor's credential staging, config-home env, and default
	// home. NewBridge injects it.
	Driver provider.Driver
	// ConfigDir is the Codex config home this bridge operates on (default
	// Driver.ConfigHome()).
	ConfigDir string
	// LockMaxWait overrides the deliver lock's bounded retry window (0 = default).
	LockMaxWait time.Duration
}

// NewBridge returns a Codex bridge configured from the driver's config home.
func NewBridge(driver provider.Driver) *Bridge {
	return &Bridge{Driver: driver, ConfigDir: driver.ConfigHome()}
}

// codexMeta is the identity sidecar the bridge writes on Add and reads on List.
type codexMeta struct {
	Email            string `json:"email"`
	AccountUUID      string `json:"accountUuid"`
	OrganizationName string `json:"organizationName"`
}

func (b *Bridge) authPath(dir string) string { return filepath.Join(dir, credentialFile) }
func (b *Bridge) disabledPath(dir string) string {
	return filepath.Join(dir, credentialFile+disabledSuffix)
}
func (b *Bridge) metaPath(dir string) string { return filepath.Join(dir, metaFile) }

// readMeta returns the identity sidecar and whether it exists (and parsed).
func (b *Bridge) readMeta(dir string) (codexMeta, bool) {
	var meta codexMeta
	raw, err := os.ReadFile(b.metaPath(dir))
	if err != nil {
		return meta, false
	}
	if err := json.Unmarshal(raw, &meta); err != nil {
		return meta, false
	}
	return meta, true
}

// checkConfigDir enforces the single-config-home rule: a per-call override may
// only ever name the bridge's own fixed config home. Any other value is a wiring
// error, not a silent second home (A-1).
func (b *Bridge) checkConfigDir(override, verb string) error {
	if b.ConfigDir == "" {
		return fmt.Errorf("codex %s: no config home configured", verb)
	}
	if override != "" && override != b.ConfigDir {
		return fmt.Errorf("codex %s: config dir override %q does not match bridge config home", verb, override)
	}
	return nil
}

// Add stages the credential set and records the account's identity sidecar. When
// req.Enable is false the account is added then immediately disabled (logged out
// but retained), matching the pool's "rotation candidate vs. parked" states.
//
// A Codex config home holds exactly one account: adding a DIFFERENT email over an
// existing one is a hard error (codex_single_account), never a silent overwrite;
// re-adding the SAME email is a credential refresh and is allowed (B-M2). The meta
// sidecar is written BEFORE auth.json so the home is never momentarily an
// email-less credential that List would have to guess at (B-H2), and any stale
// disabled credential from a prior account is cleared first so the new delivery
// wins (B-H3).
func (b *Bridge) Add(ctx context.Context, req provider.AddRequest) error {
	if err := b.checkConfigDir(req.ConfigDir, "add"); err != nil {
		return err
	}
	if req.Email == "" {
		return errors.New("codex add: empty email")
	}
	if b.Driver == nil {
		return errors.New("codex add: no provider driver configured")
	}
	dir := b.ConfigDir
	if existing, ok := b.readMeta(dir); ok && existing.Email != "" && existing.Email != req.Email {
		return fmt.Errorf("codex add: codex_single_account: config home already holds a different account")
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	// Clear a stale disabled credential from a prior account (B-H3): the new
	// delivery is authoritative and Enable must never resurrect the old one.
	if err := os.Remove(b.disabledPath(dir)); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	blob, err := json.Marshal(codexMeta{
		Email:            req.Email,
		AccountUUID:      req.AccountUUID,
		OrganizationName: req.OrganizationName,
	})
	if err != nil {
		return err
	}
	if err := writeFileAtomic(b.metaPath(dir), blob, 0o600); err != nil {
		return err
	}
	meta := provider.AddMeta{
		Email:            req.Email,
		AccountUUID:      req.AccountUUID,
		OrganizationName: req.OrganizationName,
	}
	if err := b.Driver.StageCredential(dir, req.CredentialJSON, meta); err != nil {
		return err
	}
	if req.Enable {
		return nil
	}
	return b.Disable(ctx, req.Email)
}

// ownsAccount reports whether the config home currently holds the named account.
// A verb targeting any other email is treated as "no such account" — the same
// absent-account semantics the tsamx bridge relies on (idempotent no-op) — so a
// stale or mistargeted command can never touch a different account's files (B-M1).
func (b *Bridge) ownsAccount(dir, account string) bool {
	meta, ok := b.readMeta(dir)
	return ok && meta.Email != "" && meta.Email == account
}

// Disable logs the account out by renaming auth.json to auth.json.amx-disabled.
// Idempotent: already-disabled (or absent) is a no-op success. A mismatched
// account is a no-op (B-M1).
func (b *Bridge) Disable(_ context.Context, account string) error {
	if b.ConfigDir == "" {
		return errors.New("codex disable: no config home configured")
	}
	dir := b.ConfigDir
	if !b.ownsAccount(dir, account) {
		return nil // not this account: nothing to disable
	}
	err := os.Rename(b.authPath(dir), b.disabledPath(dir))
	if errors.Is(err, os.ErrNotExist) {
		return nil // already disabled or no account: desired state already holds
	}
	return err
}

// Enable restores a disabled account by renaming auth.json.amx-disabled back.
// Idempotent: already-enabled (or absent) is a no-op success. It never overwrites
// a live auth.json: if both the disabled and live credentials exist it errors
// rather than clobber the active one (B-H3). A mismatched account is a no-op.
func (b *Bridge) Enable(_ context.Context, account string) error {
	if b.ConfigDir == "" {
		return errors.New("codex enable: no config home configured")
	}
	dir := b.ConfigDir
	if !b.ownsAccount(dir, account) {
		return nil // not this account: nothing to enable
	}
	if !fileExists(b.disabledPath(dir)) {
		return nil // already enabled or no disabled credential to restore
	}
	if fileExists(b.authPath(dir)) {
		return errors.New("codex enable: live credential present, refusing to overwrite it")
	}
	return os.Rename(b.disabledPath(dir), b.authPath(dir))
}

// Remove deletes the credential (both enabled and disabled forms) and the meta
// sidecar. Idempotent: missing files are ignored. A mismatched account is a no-op
// (B-M1).
func (b *Bridge) Remove(_ context.Context, account string) error {
	if b.ConfigDir == "" {
		return errors.New("codex remove: no config home configured")
	}
	dir := b.ConfigDir
	if !b.ownsAccount(dir, account) {
		return nil // not this account: nothing to remove
	}
	for _, p := range []string{b.authPath(dir), b.disabledPath(dir), b.metaPath(dir)} {
		if err := os.Remove(p); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	return nil
}

// Switch is a no-op: a Codex config home holds a single account, so there is
// nothing to switch between.
func (b *Bridge) Switch(context.Context, string) error { return nil }

// SwitchStrategy is a no-op for the same reason as Switch.
func (b *Bridge) SwitchStrategy(context.Context, string) error { return nil }

// AutoOnce reports "no action": with one account there is no autoswitch tick to
// run, so it returns exit code 2 (no action) with a nil error.
func (b *Bridge) AutoOnce(context.Context) (int, error) { return autoOnceNoAction, nil }

// ConfigSetThreshold is a no-op: Codex has no central autoswitch policy to set.
func (b *Bridge) ConfigSetThreshold(context.Context, float64) error { return nil }

// ConfigSet is a no-op for the same reason as ConfigSetThreshold.
func (b *Bridge) ConfigSet(context.Context, string, float64) error { return nil }

// AutoStatePath returns "" so the quarantine watcher is not started for Codex.
func (b *Bridge) AutoStatePath() string { return "" }

// ReadQuarantine returns an empty map: Codex has no autoswitch quarantine state.
func (b *Bridge) ReadQuarantine(context.Context) (map[string]string, error) {
	return map[string]string{}, nil
}

// List synthesizes the single-account pool view from on-disk state: auth.json
// present => active, auth.json.amx-disabled present => disabled. When neither
// exists the account is absent (empty result).
//
// An account is reported ONLY when the meta sidecar names it: a CODEX_HOME a human
// logged into by hand (auth.json present, no meta) is deliberately NOT surfaced, so
// downstream usage/quiescence logic can never mistake it for a managed-but-idle
// account and reclaim it (B-H2).
func (b *Bridge) List(_ context.Context) (*provider.ListResult, error) {
	dir := b.ConfigDir
	res := &provider.ListResult{SchemaVersion: 1, Accounts: []provider.AccountRow{}}
	if dir == "" {
		return res, nil
	}
	active := fileExists(b.authPath(dir))
	disabled := fileExists(b.disabledPath(dir))
	if !active && !disabled {
		return res, nil
	}
	meta, ok := b.readMeta(dir)
	if !ok || meta.Email == "" {
		return res, nil // unmanaged home (e.g. manual login): report nothing
	}
	row := provider.AccountRow{
		Number:           1,
		Email:            meta.Email,
		OrganizationName: meta.OrganizationName,
		Active:           active,
		Disabled:         disabled && !active,
		Usage:            b.readUsage(dir),
	}
	res.Accounts = append(res.Accounts, row)
	if active {
		n := 1
		res.ActiveAccountNumber = &n
	}
	return res, nil
}

// Status derives the active account from List.
func (b *Bridge) Status(ctx context.Context) (*provider.StatusResult, error) {
	list, err := b.List(ctx)
	if err != nil {
		return nil, err
	}
	res := &provider.StatusResult{ActiveAccountNumber: list.ActiveAccountNumber}
	for _, a := range list.Accounts {
		if a.Active {
			res.ActiveEmail = a.Email
			break
		}
	}
	return res, nil
}

// DeliverLock takes an exclusive, non-blocking flock over <configHome>/.amx-deliver.lock
// with a bounded retry, then falls open (returns a no-op release) so a deliver can
// never stall behind a long-lived runner holding the shared lock. Always returns a
// non-nil release. Mirrors the tsamx ExecBridge lock so the two vendors behave
// identically; the code is duplicated rather than shared to keep this package free
// of a tsamx dependency.
func (b *Bridge) DeliverLock(ctx context.Context) func() error {
	noop := func() error { return nil }
	dir := b.lockConfigHome()
	if dir == "" {
		return noop
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return noop
	}
	lockPath := filepath.Join(dir, deliverLockName)
	deadline := time.Now().Add(b.lockMaxWait())
	for {
		lock, lockErr := fslock.TryLock(lockPath)
		if lockErr == nil {
			return lock.Unlock
		}
		if !errors.Is(lockErr, fslock.ErrWouldBlock) {
			return noop
		}
		if time.Now().After(deadline) {
			return noop
		}
		select {
		case <-ctx.Done():
			return noop
		case <-time.After(deliverLockRetryInterval):
		}
	}
}

func (b *Bridge) lockConfigHome() string {
	if b.ConfigDir != "" {
		return b.ConfigDir
	}
	if b.Driver != nil {
		return b.Driver.DefaultConfigHome()
	}
	return ""
}

func (b *Bridge) lockMaxWait() time.Duration {
	if b.LockMaxWait > 0 {
		return b.LockMaxWait
	}
	return deliverLockMaxWait
}

func fileExists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

// rolloutLine is the subset of one rollout jsonl record the usage scan reads.
type rolloutLine struct {
	Type    string `json:"type"`
	Payload struct {
		Type       string `json:"type"`
		RateLimits *struct {
			Primary   *rateWindow `json:"primary"`
			Secondary *rateWindow `json:"secondary"`
		} `json:"rate_limits"`
	} `json:"payload"`
}

type rateWindow struct {
	UsedPercent   float64 `json:"used_percent"`
	WindowMinutes int     `json:"window_minutes"`
	ResetsAt      int64   `json:"resets_at"`
}

// readUsage reads the most recent rollout token_count rate_limits and reports each
// present window in the vendor-neutral Usage.Windows list (id "primary"/"secondary"
// with the event's own window_minutes). Codex windows are NOT forced into the
// Claude-shaped FiveHour/SevenDay fields — those are left nil. Returns nil when no
// usable rate_limits are found.
func (b *Bridge) readUsage(dir string) *provider.Usage {
	path := latestRollout(filepath.Join(dir, "sessions"))
	if path == "" {
		return nil
	}
	tail, err := readTail(path, usageTailBytes)
	if err != nil {
		return nil
	}
	line := lastRateLimitLine(tail)
	if line == nil || line.Payload.RateLimits == nil {
		return nil
	}
	rl := line.Payload.RateLimits
	usage := &provider.Usage{}
	if rl.Primary != nil {
		usage.Windows = append(usage.Windows, windowFrom("primary", rl.Primary))
	}
	if rl.Secondary != nil {
		usage.Windows = append(usage.Windows, windowFrom("secondary", rl.Secondary))
	}
	if len(usage.Windows) == 0 {
		return nil
	}
	return usage
}

func windowFrom(id string, w *rateWindow) provider.Window {
	out := provider.Window{Id: id, WindowMinutes: w.WindowMinutes, Pct: w.UsedPercent}
	if w.ResetsAt > 0 {
		out.ResetsAt = time.Unix(w.ResetsAt, 0).UTC().Format(time.RFC3339)
	}
	return out
}

// latestRollout walks sessionsDir for rollout-*.jsonl files and returns the path
// of the most recently modified one (empty when none exist).
func latestRollout(sessionsDir string) string {
	var newest string
	var newestMod time.Time
	_ = filepath.Walk(sessionsDir, func(path string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return nil
		}
		name := info.Name()
		if len(name) < len("rollout-") || name[:len("rollout-")] != "rollout-" {
			return nil
		}
		if filepath.Ext(name) != ".jsonl" {
			return nil
		}
		if newest == "" || info.ModTime().After(newestMod) {
			newest, newestMod = path, info.ModTime()
		}
		return nil
	})
	return newest
}

// readTail returns up to maxBytes from the end of the file.
func readTail(path string, maxBytes int64) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	fi, err := f.Stat()
	if err != nil {
		return nil, err
	}
	start := int64(0)
	if fi.Size() > maxBytes {
		start = fi.Size() - maxBytes
	}
	if _, err := f.Seek(start, io.SeekStart); err != nil {
		return nil, err
	}
	return io.ReadAll(f)
}

// lastRateLimitLine scans buf's lines from the end and returns the last record
// that is a token_count event carrying rate_limits. A leading partial line (from
// a mid-file tail cut) simply fails to parse and is skipped.
func lastRateLimitLine(buf []byte) *rolloutLine {
	lines := splitLines(buf)
	for i := len(lines) - 1; i >= 0; i-- {
		if len(lines[i]) == 0 {
			continue
		}
		var line rolloutLine
		if err := json.Unmarshal(lines[i], &line); err != nil {
			continue
		}
		if line.Payload.Type == "token_count" && line.Payload.RateLimits != nil {
			return &line
		}
	}
	return nil
}

func splitLines(buf []byte) [][]byte {
	var out [][]byte
	start := 0
	for i, c := range buf {
		if c == '\n' {
			out = append(out, buf[start:i])
			start = i + 1
		}
	}
	if start < len(buf) {
		out = append(out, buf[start:])
	}
	return out
}
