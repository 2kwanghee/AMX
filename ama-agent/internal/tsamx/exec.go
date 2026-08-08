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
)

// deliverLockName is the file flock'd for the deliver critical section (B1b). It
// is kept separate from the credential files so the runner never touches it.
const deliverLockName = ".amx-deliver.lock"

// EnvBinary names the tsamx executable when it is not on PATH (a venv shim, a
// uv-managed install). Read once by NewExecBridge.
const EnvBinary = "AMX_TSAMX_BIN"

// ExecBridge runs the real tsamx CLI against one Claude config home — the
// agent's own. Isolation between AMA instances is per *server*, not per account:
// one tsamx pool per host, so CLAUDE_CONFIG_DIR / XDG_DATA_HOME come from the
// daemon's environment and every verb sees the same pool.
//
// Unit tests use Fake instead, so a real tsamx install is not a build- or
// unit-test dependency; the E2E suite exercises this type (design note §6, §8).
type ExecBridge struct {
	// Binary is the tsamx executable (default $AMX_TSAMX_BIN, else "tsamx").
	Binary string
	// BaseEnv is the environment every invocation inherits (default os.Environ()).
	BaseEnv []string
	// ConfigDir is the Claude config home tsamx reads and captures from
	// (default $CLAUDE_CONFIG_DIR). Add stages the credential set here.
	ConfigDir string
	// DataHome is the tsamx backup root's XDG base (default $XDG_DATA_HOME).
	DataHome string
	// Timeout bounds a single CLI invocation (default 30s).
	Timeout time.Duration
}

// NewExecBridge returns an ExecBridge configured from the daemon's environment.
func NewExecBridge() *ExecBridge {
	binary := os.Getenv(EnvBinary)
	if binary == "" {
		binary = "tsamx"
	}
	return &ExecBridge{
		Binary:    binary,
		BaseEnv:   os.Environ(),
		ConfigDir: os.Getenv("CLAUDE_CONFIG_DIR"),
		DataHome:  os.Getenv("XDG_DATA_HOME"),
		Timeout:   30 * time.Second,
	}
}

func (b *ExecBridge) binary() string {
	if b.Binary == "" {
		return "tsamx"
	}
	return b.Binary
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
	if configDir != "" {
		env = append(env, "CLAUDE_CONFIG_DIR="+configDir)
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

// claudeIdentity is the subset of Claude's global config that tsamx reads to
// name the account it is about to capture (`oauthAccount` in `.claude.json`).
type claudeIdentity struct {
	OAuthAccount struct {
		EmailAddress     string `json:"emailAddress"`
		AccountUUID      string `json:"accountUuid"`
		OrganizationUUID string `json:"organizationUuid"`
		OrganizationName string `json:"organizationName"`
	} `json:"oauthAccount"`
}

// Add installs one account into the local pool (SSOT §6.3 deliver).
//
// `tsamx add` captures whatever account the Claude config home currently holds
// — it takes no identifier — so the bridge first stages that home: the
// credential set into `.credentials.json` and the identity into `.claude.json`.
// The credential travels by file, never as an argument or a log line (§7).
//
// AMS carries no organization UUID for an account, so every delivered account is
// staged as a personal one; tsamx keys slots on (email, organizationUuid) and an
// empty UUID is its personal-account value.
func (b *ExecBridge) Add(ctx context.Context, req AddRequest) error {
	configDir := req.ConfigDir
	if configDir == "" {
		configDir = b.ConfigDir
	}
	if configDir == "" {
		return fmt.Errorf("tsamx add %s: no Claude config home configured (set CLAUDE_CONFIG_DIR)", req.Email)
	}
	if err := os.MkdirAll(configDir, 0o700); err != nil {
		return err
	}
	// Write both files atomically (temp in the same dir + rename). The runner
	// (Claude Code) reads these concurrently; a non-atomic os.WriteFile could be
	// observed half-written, so an in-flight runner request would read a torn
	// credential. os.Rename is atomic on the same filesystem.
	if err := writeFileAtomic(filepath.Join(configDir, ".credentials.json"), req.CredentialJSON, 0o600); err != nil {
		return err
	}
	var identity claudeIdentity
	identity.OAuthAccount.EmailAddress = req.Email
	identity.OAuthAccount.AccountUUID = req.AccountUUID
	identity.OAuthAccount.OrganizationName = req.OrganizationName
	blob, err := json.Marshal(identity)
	if err != nil {
		return err
	}
	if err := writeFileAtomic(filepath.Join(configDir, ".claude.json"), blob, 0o600); err != nil {
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

// writeFileAtomic writes data to a temp file in the same directory as path and
// renames it into place, so a concurrent reader (the runner) never observes a
// partial write. The temp file is created 0o600 and the final file carries perm;
// on any failure before the rename the temp file is removed. Never logs data.
func writeFileAtomic(path string, data []byte, perm os.FileMode) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".amx-*.tmp")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	// Best-effort cleanup unless the rename below claims the temp file.
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
	tmpName = "" // renamed into place; skip cleanup
	return nil
}

// DeliverLock implements Bridge.DeliverLock: an exclusive flock (LOCK_EX) over
// <configDir>/.amx-deliver.lock, held for the deliver critical section (B1b). The
// amx-claude wrapper takes a shared lock (LOCK_SH) on the same path before it
// reads the credential, so it blocks for the (sub-second) swap rather than
// reading a half-swapped or momentarily-new credential. When no config home is
// configured there is nothing to protect, so the release is a no-op.
func (b *ExecBridge) DeliverLock(_ context.Context) (func() error, error) {
	configDir := b.ConfigDir
	if configDir == "" {
		return func() error { return nil }, nil
	}
	if err := os.MkdirAll(configDir, 0o700); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(filepath.Join(configDir, deliverLockName), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX); err != nil {
		_ = f.Close()
		return nil, err
	}
	return func() error {
		// Closing the fd releases the lock; unlock explicitly first for clarity.
		unlockErr := syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
		closeErr := f.Close()
		if unlockErr != nil {
			return unlockErr
		}
		return closeErr
	}, nil
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
func (b *ExecBridge) List(ctx context.Context) (*ListResult, error) {
	out, err := b.run(ctx, b.env("", ""), "list", "--json")
	if err != nil {
		return nil, err
	}
	var res ListResult
	if err := json.Unmarshal(out, &res); err != nil {
		return nil, fmt.Errorf("parse tsamx list --json: %w", err)
	}
	return &res, nil
}

// Status runs `tsamx status --json`. tsamx does not emit a stable top-level
// status schema this narrow, so AMA derives active-account from `list` and keeps
// Status as a thin convenience over the same read.
func (b *ExecBridge) Status(ctx context.Context) (*StatusResult, error) {
	list, err := b.List(ctx)
	if err != nil {
		return nil, err
	}
	res := &StatusResult{ActiveAccountNumber: list.ActiveAccountNumber}
	for _, a := range list.Accounts {
		if a.Active {
			res.ActiveEmail = a.Email
			break
		}
	}
	return res, nil
}
