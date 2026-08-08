package tsamx

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"time"
)

// ExecBridge runs the real tsamx CLI. It is a P2 skeleton: List/Status parse
// `--json` (the reporter's read path, exercised end to end), while the mutating
// verbs exec the corresponding subcommand with per-account env injection.
//
// Tests never use this type — they use Fake — so a real tsamx install is not a
// build- or unit-test dependency (design note §6, §8).
type ExecBridge struct {
	// Binary is the tsamx executable (default "tsamx").
	Binary string
	// BaseEnv is the environment before per-account injection (default os.Environ()).
	BaseEnv []string
	// Timeout bounds a single CLI invocation (default 30s).
	Timeout time.Duration
}

// NewExecBridge returns an ExecBridge with defaults filled in.
func NewExecBridge() *ExecBridge {
	return &ExecBridge{Binary: "tsamx", BaseEnv: os.Environ(), Timeout: 30 * time.Second}
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

// env builds the process environment with optional per-account isolation.
func (b *ExecBridge) env(configDir, dataHome string) []string {
	base := b.BaseEnv
	if base == nil {
		base = os.Environ()
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

// Add writes the credential set into the account's config home, then runs
// `tsamx add`. Writing the credential file mirrors SSOT §6.3 (deliver installs
// ~/.claude/.credentials.json before `tsamx add`).
//
// NOTE(P2 skeleton): the exact `tsamx add` argument surface for a pre-seeded
// credential is finalized in the deployment track (O5). This installs the
// credential file and invokes the verb; the credential is passed via file, never
// as a CLI arg or a log line.
func (b *ExecBridge) Add(ctx context.Context, req AddRequest) error {
	if req.ConfigDir != "" {
		if err := os.MkdirAll(req.ConfigDir, 0o700); err != nil {
			return err
		}
		credPath := filepath.Join(req.ConfigDir, ".credentials.json")
		if err := os.WriteFile(credPath, req.CredentialJSON, 0o600); err != nil {
			return err
		}
	}
	env := b.env(req.ConfigDir, req.DataHome)
	if _, err := b.run(ctx, env, "add", req.Email, "--json"); err != nil {
		return err
	}
	if req.Enable {
		return b.Enable(ctx, req.Email)
	}
	return b.Disable(ctx, req.Email)
}

// Remove runs `tsamx remove <account>`.
func (b *ExecBridge) Remove(ctx context.Context, account string) error {
	_, err := b.run(ctx, b.env("", ""), "remove", account, "--json")
	return err
}

// Enable runs `tsamx enable <account>`.
func (b *ExecBridge) Enable(ctx context.Context, account string) error {
	_, err := b.run(ctx, b.env("", ""), "enable", account, "--json")
	return err
}

// Disable runs `tsamx disable <account>`.
func (b *ExecBridge) Disable(ctx context.Context, account string) error {
	_, err := b.run(ctx, b.env("", ""), "disable", account, "--json")
	return err
}

// Switch runs `tsamx switch <target>`.
func (b *ExecBridge) Switch(ctx context.Context, target string) error {
	_, err := b.run(ctx, b.env("", ""), "switch", target, "--json")
	return err
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
