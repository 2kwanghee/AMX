package tsamx

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"syscall"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
)

// ExecBridge implements the provider.Bridge control surface via the tsamx CLI.
var _ provider.Bridge = (*ExecBridge)(nil)

// deliverLockName is the file flock'd for the deliver critical section (B1b). It
// is kept separate from the credential files so the runner never touches it.
const deliverLockName = ".amx-deliver.lock"

// deliverLockMaxWait bounds how long DeliverLock retries a non-blocking flock
// before proceeding WITHOUT the lock (fail-open). A runner (amx-claude) may hold
// a shared lock; waiting is bounded so a deliver can never block indefinitely.
// B1a (previous-active restore + atomic write) still bounds exposure on fail-open.
const deliverLockMaxWait = 5 * time.Second

// deliverLockRetryInterval is the poll gap between non-blocking flock attempts.
const deliverLockRetryInterval = 50 * time.Millisecond

// ExecBridge runs the real tsamx CLI against one provider config home — the
// agent's own. Isolation between AMA instances is per *server*, not per account:
// one tsamx pool per host, so the config home / XDG_DATA_HOME come from the
// daemon's environment and every verb sees the same pool. All vendor-specific
// knowledge (config-home env, credential staging, pool binary) lives in Driver.
//
// Unit tests use Fake instead, so a real tsamx install is not a build- or
// unit-test dependency; the E2E suite exercises this type (design note §6, §8).
type ExecBridge struct {
	// Driver owns the vendor's credential staging, config-home env, and pool
	// binary. NewExecBridge injects it; the bridge itself is vendor-neutral.
	Driver provider.Driver
	// Binary overrides the pool executable (default Driver.BinaryName()).
	Binary string
	// BaseEnv is the environment every invocation inherits (default os.Environ()).
	BaseEnv []string
	// ConfigDir is the provider config home tsamx reads and captures from
	// (default Driver.ConfigHome()). Add stages the credential set here.
	ConfigDir string
	// DataHome is the tsamx backup root's XDG base (default $XDG_DATA_HOME).
	DataHome string
	// Timeout bounds a single CLI invocation (default 30s).
	Timeout time.Duration
	// LockMaxWait overrides the deliver lock's bounded retry window (0 = default
	// deliverLockMaxWait). Tests set a short value to exercise the fail-open path
	// quickly; production leaves it at the default.
	LockMaxWait time.Duration
}

func (b *ExecBridge) lockMaxWait() time.Duration {
	if b.LockMaxWait > 0 {
		return b.LockMaxWait
	}
	return deliverLockMaxWait
}

// NewExecBridge returns an ExecBridge for the given driver, configured from the
// daemon's environment (config home via the driver, tsamx backup root via
// XDG_DATA_HOME).
func NewExecBridge(driver provider.Driver) *ExecBridge {
	return &ExecBridge{
		Driver:    driver,
		BaseEnv:   os.Environ(),
		ConfigDir: driver.ConfigHome(),
		DataHome:  os.Getenv("XDG_DATA_HOME"),
		Timeout:   30 * time.Second,
	}
}

func (b *ExecBridge) binary() string {
	if b.Binary != "" {
		return b.Binary
	}
	if b.Driver != nil {
		return b.Driver.BinaryName()
	}
	return "tsamx"
}

func (b *ExecBridge) timeout() time.Duration {
	if b.Timeout == 0 {
		return 30 * time.Second
	}
	return b.Timeout
}

// env builds the process environment, letting a per-call override win over the
// bridge-wide config home.
func (b *ExecBridge) env(configDir, dataHome string) []string {
	base := b.BaseEnv
	if base == nil {
		base = os.Environ()
	}
	if configDir == "" {
		configDir = b.ConfigDir
	}
	if dataHome == "" {
		dataHome = b.DataHome
	}
	env := append([]string(nil), base...)
	if configDir != "" && b.Driver != nil {
		env = append(env, b.Driver.Env(configDir)...)
	}
	if dataHome != "" {
		env = append(env, "XDG_DATA_HOME="+dataHome)
	}
	return env
}

// run execs `tsamx <args...>` and returns stdout. stderr is captured for error
// context only. Never pass credential material as an argument.
func (b *ExecBridge) run(ctx context.Context, env []string, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, b.timeout())
	defer cancel()
	cmd := exec.CommandContext(ctx, b.binary(), args...)
	cmd.Env = env
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return nil, fmt.Errorf("tsamx %v: %w (stderr: %s)", args, err, stderr.String())
	}
	return stdout.Bytes(), nil
}

// Add installs one account into the local pool (SSOT §6.3 deliver).
//
// `tsamx add` captures whatever account the config home currently holds — it
// takes no identifier — so the bridge first has the driver stage that home
// (credential set + identity). The credential travels by file, never as an
// argument or a log line (§7).
func (b *ExecBridge) Add(ctx context.Context, req provider.AddRequest) error {
	configDir := req.ConfigDir
	if configDir == "" {
		configDir = b.ConfigDir
	}
	if configDir == "" {
		return fmt.Errorf("tsamx add %s: no config home configured", req.Email)
	}
	if b.Driver == nil {
		return fmt.Errorf("tsamx add %s: no provider driver configured", req.Email)
	}
	meta := provider.AddMeta{
		Email:            req.Email,
		AccountUUID:      req.AccountUUID,
		OrganizationName: req.OrganizationName,
	}
	if err := b.Driver.StageCredential(configDir, req.CredentialJSON, meta); err != nil {
		return err
	}

	env := b.env(configDir, req.DataHome)
	if _, err := b.run(ctx, env, "add"); err != nil {
		return err
	}
	// `add` makes the new slot active and enabled; only a disabled desired state
	// needs a follow-up verb.
	if req.Enable {
		return nil
	}
	return b.Disable(ctx, req.Email)
}

// lockConfigHome resolves the config home the deliver lock lives in. It mirrors
// the vendor runner wrapper's default (the bridge's config home, else the
// driver's conventional home) so both sides flock the SAME file even when the
// daemon has no explicit ConfigDir — otherwise the flock would be silently
// ineffective (B1b review item 3).
func (b *ExecBridge) lockConfigHome() string {
	if b.ConfigDir != "" {
		return b.ConfigDir
	}
	if b.Driver != nil {
		return b.Driver.DefaultConfigHome()
	}
	return ""
}

// DeliverLock implements Bridge.DeliverLock: it takes an exclusive flock
// (LOCK_EX) over <configHome>/.amx-deliver.lock for the deliver critical section
// (B1b) and returns a release func. The amx-claude wrapper takes a shared lock on
// the same path before it starts claude, so a runner cannot begin inside the swap
// window and read a half-swapped or momentarily-new credential.
//
// Crucially the acquisition is NON-BLOCKING with a bounded retry (LOCK_EX|LOCK_NB,
// polled up to deliverLockMaxWait) and is called by handleDeliver OUTSIDE the
// engine lock. If the lock cannot be taken within the bound — e.g. a long-lived
// interactive runner is holding the shared lock — DeliverLock gives up and returns
// a no-op release so the deliver proceeds WITHOUT it (fail-open, availability
// first). It therefore never blocks the engine, and every path returns a usable
// release (never nil), so the caller can always defer it.
func (b *ExecBridge) DeliverLock(ctx context.Context) func() error {
	noop := func() error { return nil }
	configDir := b.lockConfigHome()
	if configDir == "" {
		return noop
	}
	if err := os.MkdirAll(configDir, 0o700); err != nil {
		return noop // fail-open: cannot create the home, proceed unlocked
	}
	f, err := os.OpenFile(filepath.Join(configDir, deliverLockName), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return noop
	}
	deadline := time.Now().Add(b.lockMaxWait())
	for {
		lockErr := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if lockErr == nil {
			return func() error {
				unlockErr := syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
				closeErr := f.Close()
				if unlockErr != nil {
					return unlockErr
				}
				return closeErr
			}
		}
		if !errors.Is(lockErr, syscall.EWOULDBLOCK) {
			_ = f.Close() // unexpected flock failure: fail-open
			return noop
		}
		if time.Now().After(deadline) {
			_ = f.Close() // could not acquire within the bound: fail-open
			return noop
		}
		select {
		case <-ctx.Done():
			_ = f.Close()
			return noop
		case <-time.After(deliverLockRetryInterval):
		}
	}
}

// Remove runs `tsamx remove <account>`.
func (b *ExecBridge) Remove(ctx context.Context, account string) error {
	_, err := b.run(ctx, b.env("", ""), "remove", account)
	return err
}

// Enable runs `tsamx enable <account>`.
func (b *ExecBridge) Enable(ctx context.Context, account string) error {
	_, err := b.run(ctx, b.env("", ""), "enable", account)
	return err
}

// Disable runs `tsamx disable <account>`.
func (b *ExecBridge) Disable(ctx context.Context, account string) error {
	_, err := b.run(ctx, b.env("", ""), "disable", account)
	return err
}

// Switch runs `tsamx switch <target>`.
func (b *ExecBridge) Switch(ctx context.Context, target string) error {
	_, err := b.run(ctx, b.env("", ""), "switch", target)
	return err
}

// SwitchStrategy runs `tsamx switch --strategy <strategy>` ("best" or
// "next-available"), letting tsamx rank the candidate accounts itself.
func (b *ExecBridge) SwitchStrategy(ctx context.Context, strategy string) error {
	_, err := b.run(ctx, b.env("", ""), "switch", "--strategy", strategy)
	return err
}

// ConfigSetThreshold runs `tsamx config set autoswitch.threshold <pct>`.
func (b *ExecBridge) ConfigSetThreshold(ctx context.Context, pct float64) error {
	// Format compactly (no trailing zeros); tsamx accepts an integer or decimal.
	val := strconv.FormatFloat(pct, 'g', -1, 64)
	_, err := b.run(ctx, b.env("", ""), "config", "set", "autoswitch.threshold", val)
	return err
}

// ConfigSet runs `tsamx config set autoswitch.<key> <value>` for the F4 (O4-B)
// central policy fields (cooldown_seconds, hysteresis_pct).
func (b *ExecBridge) ConfigSet(ctx context.Context, key string, value float64) error {
	// Format compactly (no trailing zeros); tsamx accepts an integer or decimal.
	val := strconv.FormatFloat(value, 'g', -1, 64)
	_, err := b.run(ctx, b.env("", ""), "config", "set", "autoswitch."+key, val)
	return err
}

// AutoOnce runs `tsamx auto --once` and returns the CLI's exit code. `auto` must
// be the first argument (tsamx pre-dispatches it), so it is passed verbatim.
// Exit codes: 0 switched, 2 no action, 3 blocked. Exit 1 (error) and any
// failure to run are surfaced as a non-nil error.
func (b *ExecBridge) AutoOnce(ctx context.Context) (int, error) {
	ctx, cancel := context.WithTimeout(ctx, b.timeout())
	defer cancel()
	cmd := exec.CommandContext(ctx, b.binary(), "auto", "--once")
	cmd.Env = b.env("", "")
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	err := cmd.Run()
	if err == nil {
		return 0, nil
	}
	var ee *exec.ExitError
	if errors.As(err, &ee) {
		code := ee.ExitCode()
		if code == 1 {
			return 1, fmt.Errorf("tsamx auto --once: error (stderr: %s)", stderr.String())
		}
		// 2 (no action) and 3 (blocked / all exhausted) are normal outcomes.
		return code, nil
	}
	return -1, fmt.Errorf("tsamx auto --once: %w (stderr: %s)", err, stderr.String())
}

// AutoStatePath resolves tsamx's autoswitch_state.json. On Linux/WSL tsamx keeps
// its backup root at $XDG_DATA_HOME/tsamx (default ~/.local/share/tsamx); the
// bridge mirrors that resolution from its own DataHome/env.
func (b *ExecBridge) AutoStatePath() string {
	root := b.backupRoot()
	if root == "" {
		return ""
	}
	return filepath.Join(root, "autoswitch_state.json")
}

func (b *ExecBridge) backupRoot() string {
	dataHome := b.DataHome
	if dataHome == "" {
		dataHome = os.Getenv("XDG_DATA_HOME")
	}
	if dataHome != "" {
		return filepath.Join(dataHome, "tsamx")
	}
	if home, err := os.UserHomeDir(); err == nil && home != "" {
		return filepath.Join(home, ".local", "share", "tsamx")
	}
	return ""
}

// autoState is the subset of autoswitch_state.json AMA reads: the quarantine map
// keyed by slot number, each entry carrying the account email.
type autoState struct {
	Quarantine map[string]struct {
		Email string `json:"email"`
	} `json:"quarantine"`
}

// ReadQuarantine parses autoswitch_state.json and returns number->email for the
// quarantined accounts. A missing/unreadable file means nothing is quarantined.
func (b *ExecBridge) ReadQuarantine(_ context.Context) (map[string]string, error) {
	path := b.AutoStatePath()
	out := make(map[string]string)
	if path == "" {
		return out, nil
	}
	blob, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return out, nil
		}
		return out, nil // unreadable/partial write -> treat as empty (design note §2)
	}
	var st autoState
	if err := json.Unmarshal(blob, &st); err != nil {
		return out, nil
	}
	for num, entry := range st.Quarantine {
		out[num] = entry.Email
	}
	return out, nil
}

// List runs `tsamx list --json` and parses the schema-v1 payload.
func (b *ExecBridge) List(ctx context.Context) (*provider.ListResult, error) {
	out, err := b.run(ctx, b.env("", ""), "list", "--json")
	if err != nil {
		return nil, err
	}
	var res provider.ListResult
	if err := json.Unmarshal(out, &res); err != nil {
		return nil, fmt.Errorf("parse tsamx list --json: %w", err)
	}
	return &res, nil
}

// Status runs `tsamx status --json`. tsamx does not emit a stable top-level
// status schema this narrow, so AMA derives active-account from `list` and keeps
// Status as a thin convenience over the same read.
func (b *ExecBridge) Status(ctx context.Context) (*provider.StatusResult, error) {
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
