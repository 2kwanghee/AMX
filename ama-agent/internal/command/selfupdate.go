package command

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/store"
	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
)

// DefaultSelfUpdateMinFreeBytes is the free space the working tree must have
// before a self update is attempted. A `go build` of this module needs room for
// the build cache plus a second copy of the binary; running out mid-build leaves
// a truncated ama.new, so the preflight refuses instead.
const DefaultSelfUpdateMinFreeBytes = 512 << 20 // 512 MiB

// Per-step timeouts. Without them a hung git (an unreachable remote that never
// resets the connection, a credential prompt on a tty-less process) or a wedged
// linker parks the self update forever, and because the command holds no lock
// until the swap the operator sees an agent that simply never acks. Each bound is
// generous for the honest case: a fast-forward of this repo is seconds, a cold
// `go build` of the agent is a couple of minutes, and `--version` is immediate.
const (
	gitStepTimeout   = 120 * time.Second
	buildStepTimeout = 600 * time.Second
	smokeStepTimeout = 15 * time.Second
)

// newBinarySuffix / backupSuffix name the two extra files a self update creates
// next to the running binary. Both live in the same directory so the swap is a
// same-filesystem os.Rename (atomic); a cross-filesystem staging directory would
// silently degrade to copy+truncate and could leave no working binary at all.
const (
	newBinarySuffix = ".new"
	backupSuffix    = ".bak"
)

// ErrRunTimeout marks a step killed by its own deadline, so the handler can ack
// "timeout_<step>" instead of burying it in a generic failure.
var ErrRunTimeout = errors.New("command: step timed out")

// RunSpec is one child process a self update runs.
type RunSpec struct {
	Dir  string
	Name string
	Args []string
	// Env is the child's complete environment. Nil inherits the agent's, which is
	// right for git and go; the smoke test deliberately passes a whitelist instead
	// (see smokeEnv).
	Env []string
	// Timeout bounds the run. 0 means no bound.
	Timeout time.Duration
}

// SelfUpdateRunner is the OS surface handleSelfUpdate touches. It exists so the
// handler can be tested against a fake process/filesystem without a real build
// or a real exec (which would replace the test binary).
type SelfUpdateRunner interface {
	// LookPath resolves an executable on PATH (preflight: the Go toolchain).
	LookPath(file string) (string, error)
	// FreeBytes reports the free space of the filesystem holding dir.
	FreeBytes(dir string) (uint64, error)
	// Run executes spec and returns its combined output. A non-zero exit is an
	// error; the output is folded into it for the ack detail. A run killed by
	// spec.Timeout returns an error wrapping ErrRunTimeout.
	Run(ctx context.Context, spec RunSpec) (string, error)
	// Rename moves a path within one filesystem (os.Rename semantics).
	Rename(oldpath, newpath string) error
	// Remove deletes a path, ignoring "not exist".
	Remove(name string) error
	// Exec replaces the current process image (syscall.Exec). It only ever
	// returns on failure.
	Exec(argv0 string, argv, envv []string) error
}

// SelfUpdateConfig enables the self_update command. When it is absent from the
// Handler's Config the command is rejected as unsupported rather than silently
// acked, so an operator who issues it against an agent that cannot update sees
// the reason.
type SelfUpdateConfig struct {
	// RepoDir is the git working tree to fast-forward. It must be a clone the
	// operator configured; the command never names a source (proto SelfUpdate).
	RepoDir string
	// ModuleDir is where `go build ./cmd/ama` runs. Empty -> RepoDir/ama-agent.
	ModuleDir string
	// BinaryPath is the running binary that gets replaced. Empty -> os.Executable().
	BinaryPath string
	// MinFreeBytes is the preflight free-space floor. 0 -> DefaultSelfUpdateMinFreeBytes.
	MinFreeBytes uint64
	// Runner is the OS surface. Nil -> OSSelfUpdateRunner.
	Runner SelfUpdateRunner
	// AckSender delivers the CONVERGED ack to AMS from inside the critical
	// section, before the process is replaced. It is best-effort by nature (see
	// handleSelfUpdate); production wires it to a send that waits briefly for the
	// transport's write confirmation. Nil skips the in-band send and returns the
	// ack to the caller instead (tests); when it is set, Handle returns nil for a
	// command whose ack it has already sent.
	AckSender func(*amxv1.CommandAck) error
	// Logf records what happens after the ack, when there is no longer an ack to
	// carry the news. Nil discards.
	Logf func(format string, args ...any)
}

func (c *SelfUpdateConfig) moduleDir() string {
	if c.ModuleDir != "" {
		return c.ModuleDir
	}
	return filepath.Join(c.RepoDir, "ama-agent")
}

func (c *SelfUpdateConfig) minFree() uint64 {
	if c.MinFreeBytes > 0 {
		return c.MinFreeBytes
	}
	return DefaultSelfUpdateMinFreeBytes
}

func (c *SelfUpdateConfig) logf(format string, args ...any) {
	if c.Logf != nil {
		c.Logf(format, args...)
	}
}

// smokeEnv is the whole environment the freshly built binary gets for its
// version check: enough to run a dynamically linked Go program, and nothing that
// could let it behave like an agent. AMX_AGENT_ID / AMX_AMS_ADDR / AMX_ENROLL_TOKEN
// are all absent, so a binary built from a commit that does not understand
// --version cannot quietly come up as a SECOND live agent and register a
// duplicate session against the one that is running.
func smokeEnv() []string {
	out := make([]string, 0, 2)
	for _, k := range []string{"PATH", "HOME"} {
		if v := os.Getenv(k); v != "" {
			out = append(out, k+"="+v)
		}
	}
	return out
}

// handleSelfUpdate rebuilds the agent from its own working tree and restarts
// into the new binary (§6.3 self_update).
//
// Ordering is the whole design. Everything that can fail — fetch, pin check,
// pull, build, smoke test — runs OUTSIDE the engine lock, and every step up to
// and including the pin check leaves the working tree and the installed binary
// exactly as they were: a failure there acks DIVERGED and the agent keeps
// serving from the binary it already had. Only once a validated binary exists is
// the engine lock taken, and under it the sequence is short and irreversible:
// back up, swap, durably record the command as applied, ack, exec.
//
// The applied-log append must reach disk BEFORE the exec. AMS re-queues a
// command it has no ack for, and the restarted process answers that re-queue
// from the log (alreadyApplied -> CONVERGED, no rebuild). Losing the append
// would turn every re-queue into another build-and-restart, i.e. a boot loop.
// That re-queue path is also what makes the pre-exec ack best-effort rather than
// load-bearing: if the ack never reaches AMS, the next queued copy of the same
// command_id is answered CONVERGED from the log.
func (h *Handler) handleSelfUpdate(ctx context.Context, cmd *amxv1.AmsCommand, su *amxv1.SelfUpdate, ack *amxv1.CommandAck) *amxv1.CommandAck {
	cfg := h.selfUpdate
	if cfg == nil || cfg.RepoDir == "" {
		return h.finishSelfUpdate(reject(ack, "unsupported_command", errors.New("self_update is not configured on this agent")))
	}
	// Idempotency (§3): a re-queued self_update whose predecessor already
	// converged re-acks without rebuilding or restarting. This is the path the
	// process takes after the exec, so it must come before any work.
	if h.alreadyApplied(cmd.GetCommandId()) {
		return h.finishSelfUpdate(converged(ack))
	}

	runner := cfg.Runner
	if runner == nil {
		runner = OSSelfUpdateRunner{}
	}
	binPath := cfg.BinaryPath
	if binPath == "" {
		p, err := os.Executable()
		if err != nil {
			return h.finishSelfUpdate(h.divergedSelfUpdate(ack, "preflight_failed", err))
		}
		binPath = p
	}
	// fail returns a DIVERGED ack, mapping a deadline kill onto timeout_<step> so
	// a hung git and a git that genuinely refused are distinguishable in the ack.
	fail := func(step, code string, err error) *amxv1.CommandAck {
		if errors.Is(err, ErrRunTimeout) {
			code = "timeout_" + step
		}
		return h.finishSelfUpdate(h.divergedSelfUpdate(ack, code, err))
	}
	git := func(args ...string) (string, error) {
		out, err := runner.Run(ctx, RunSpec{Dir: cfg.RepoDir, Name: "git", Args: args, Timeout: gitStepTimeout})
		return strings.TrimSpace(out), err
	}

	// --- Preflight. Cheap checks that make a mid-update failure unlikely. -----
	if _, err := runner.LookPath("go"); err != nil {
		return fail("preflight", "preflight_failed", fmt.Errorf("go toolchain not on PATH: %w", err))
	}
	free, err := runner.FreeBytes(cfg.RepoDir)
	if err != nil {
		return fail("preflight", "preflight_failed", fmt.Errorf("free space: %w", err))
	}
	if free < cfg.minFree() {
		return fail("preflight", "preflight_failed",
			fmt.Errorf("free space %d bytes below the %d byte floor", free, cfg.minFree()))
	}

	// --- Fetch and check the pin BEFORE touching the working tree. ------------
	// The pin is a veto on what the agent is about to become, so it has to be
	// evaluated against the remote tip while the tree is still untouched. Checking
	// it after the pull would leave the operator's clone already advanced to a
	// commit they explicitly refused.
	//
	// `git fetch` with no remote/refspec argument only updates the tracking refs
	// of the upstream this clone is already configured for — the command names no
	// source, and this must not become a way to name one.
	if out, ferr := git("fetch", "--quiet"); ferr != nil {
		return fail("git", "git_fetch_failed", fmt.Errorf("%w: %s", ferr, out))
	}
	// @{u} is the configured upstream of the current branch. A clone with no
	// upstream cannot self-update at all, and saying so beats a confusing pull
	// error later.
	remoteTip, err := git("rev-parse", "@{u}")
	if err != nil {
		return fail("git", "no_upstream", fmt.Errorf("no upstream for the current branch: %w", err))
	}
	want := strings.TrimSpace(su.GetExpectedCommit())
	if want != "" && !commitMatches(remoteTip, want) {
		return fail("git", "commit_mismatch",
			fmt.Errorf("expected_commit %q but the upstream tip is %q (nothing fetched into the tree)", want, remoteTip))
	}

	oldCommit, err := git("rev-parse", "HEAD")
	if err != nil {
		return fail("git", "preflight_failed", fmt.Errorf("git rev-parse HEAD: %w", err))
	}

	// --ff-only, and no remote/refspec argument: the pull can only advance to
	// whatever the clone's own configured upstream holds. A non-fast-forward
	// (local commits, force-pushed upstream) fails here rather than rewriting the
	// operator's tree.
	if out, perr := git("pull", "--ff-only"); perr != nil {
		return fail("git", "git_pull_failed", fmt.Errorf("%w: %s", perr, out))
	}

	newCommit, err := git("rev-parse", "HEAD")
	if err != nil {
		return fail("git", "git_pull_failed", fmt.Errorf("git rev-parse after pull: %w", err))
	}
	// Defense in depth: the pin was already vetted against the remote tip, so a
	// mismatch here means the tree landed somewhere neither side asked for (a
	// concurrent local commit, a hook that moved HEAD). Still before the build, so
	// still nothing swapped.
	if want != "" && !commitMatches(newCommit, want) {
		return fail("git", "commit_mismatch",
			fmt.Errorf("expected_commit %q but HEAD is %q after the pull (was %q)", want, newCommit, oldCommit))
	}

	// --- Build and prove the artifact runs. ----------------------------------
	newBin := binPath + newBinarySuffix
	_ = runner.Remove(newBin) // a leftover from an aborted earlier attempt
	if out, berr := runner.Run(ctx, RunSpec{
		Dir:     cfg.moduleDir(),
		Name:    "go",
		Args:    []string{"build", "-ldflags", "-X main.commit=" + newCommit, "-o", newBin, "./cmd/ama"},
		Timeout: buildStepTimeout,
	}); berr != nil {
		_ = runner.Remove(newBin)
		return fail("build", "build_failed", fmt.Errorf("%w: %s", berr, tail(out)))
	}

	// Smoke test. Three conditions, all required: it terminates inside
	// smokeStepTimeout, it exits 0, and it prints the commit it was built from.
	// The third is what makes this more than a liveness check — a commit that
	// predates the --version flag would treat it as an unknown argument and come
	// up as a full agent, which is both a duplicate registration and a binary that
	// can never be self-updated again. Such a build either hangs (killed by the
	// timeout, having found no AMS in smokeEnv) or prints nothing with the commit
	// in it, and is refused either way.
	smokeOut, serr := runner.Run(ctx, RunSpec{
		Dir:     cfg.moduleDir(),
		Name:    newBin,
		Args:    []string{"--version"},
		Env:     smokeEnv(),
		Timeout: smokeStepTimeout,
	})
	if serr != nil {
		_ = runner.Remove(newBin)
		return fail("smoke", "smoke_failed", fmt.Errorf("%w: %s", serr, tail(smokeOut)))
	}
	if short := shortCommit(newCommit); !strings.Contains(smokeOut, short) {
		_ = runner.Remove(newBin)
		return fail("smoke", "smoke_failed",
			fmt.Errorf("--version output does not name the built commit %s (does this commit predate the flag?): %s", short, tail(smokeOut)))
	}

	// --- Swap + restart. Under the engine lock so no tsamx mutation is in
	// flight when the process image is replaced. ------------------------------
	h.engine.Lock()
	defer h.engine.Unlock()

	backup := binPath + backupSuffix
	_ = runner.Remove(backup)
	if rerr := runner.Rename(binPath, backup); rerr != nil {
		_ = runner.Remove(newBin)
		return h.finishSelfUpdate(h.divergedSelfUpdate(ack, "backup_failed", rerr))
	}
	if rerr := runner.Rename(newBin, binPath); rerr != nil {
		// Put the old binary back: without this the agent has no binary at all
		// and a supervisor restart would never come up again.
		if back := runner.Rename(backup, binPath); back != nil {
			return h.finishSelfUpdate(h.divergedSelfUpdate(ack, "install_failed",
				fmt.Errorf("%w (restore also failed: %v — recover with %s)", rerr, back, manualRecoveryHint)))
		}
		_ = runner.Remove(newBin)
		return h.finishSelfUpdate(h.divergedSelfUpdate(ack, "install_failed", rerr))
	}

	// Durable record before the exec (see the function comment). A failed append
	// is fatal to the update, not to the agent: the new binary is already
	// installed, so we roll the swap back and stay on the old one.
	converged(ack)
	ack.Detail = "restarting into " + shortCommit(newCommit)
	if aerr := h.applied.Append(store.AppliedEntry{
		CommandID:   ack.CommandId,
		Kind:        "self_update",
		Target:      newCommit,
		Desired:     "restart",
		Convergence: ack.Convergence.String(),
		AppliedAt:   h.now().UTC(),
	}); aerr != nil {
		if back := runner.Rename(backup, binPath); back == nil {
			return h.finishSelfUpdate(h.divergedSelfUpdate(ack, "applied_log_failed", aerr))
		}
		return h.finishSelfUpdate(h.divergedSelfUpdate(ack, "applied_log_failed",
			fmt.Errorf("%w (rollback failed — recover with %s)", aerr, manualRecoveryHint)))
	}

	// Ack before exec so AMS usually learns of the convergence on the session that
	// issued the command. This is best-effort and nothing depends on it: the send
	// may not have left the process by the time exec replaces it, and the honest
	// guarantee is the applied log above plus the re-queue path.
	//
	// Note what CONVERGED means here — the binary on disk was replaced and the
	// restart was requested. It is NOT a statement that the new version came up.
	// Only the agent_version on the next Register proves that.
	if cfg.AckSender != nil {
		if serr := cfg.AckSender(ack); serr != nil {
			cfg.logf("self_update %s: ack send failed (%v); AMS will re-queue and get CONVERGED from the applied log", ack.CommandId, serr)
		}
		ack = nil // already delivered; main must not send it twice
	}

	// syscall.Exec does not return on success — the process image is gone here.
	// A nil return therefore only happens under a test double standing in for the
	// replacement, in which case the CONVERGED result above still stands.
	if execErr := runner.Exec(binPath, os.Args, os.Environ()); execErr != nil {
		// The swap SUCCEEDED and is durably recorded as CONVERGED; only the restart
		// did not happen. Do not retract that ack — a DIVERGED re-ack would tell AMS
		// the update failed when the new binary is installed and will be picked up by
		// the next restart, and it would also un-suppress the re-queue that the
		// applied log is there to absorb. Log it and keep serving from the old image.
		cfg.logf("self_update %s: exec failed (%v) — the new binary IS installed at %s "+
			"and takes effect on the next restart; still running the previous image",
			ack.GetCommandId(), execErr, binPath)
	}
	return h.finishSelfUpdate(ack)
}

// manualRecoveryHint is the documented operator recovery for a half-swapped
// binary (docs/AMX-DESIGN.md §6.3 self_update).
const manualRecoveryHint = "git -C ~/AMX reset --hard origin/main && bash deploy/agent-run.sh up"

// finishSelfUpdate delivers a terminal self_update ack. When an AckSender is
// configured the ack goes out here and nil is returned, so the caller's send
// path stays a single place and no ack is ever sent twice. A nil ack (already
// delivered before the exec) passes straight through.
func (h *Handler) finishSelfUpdate(ack *amxv1.CommandAck) *amxv1.CommandAck {
	if h.selfUpdate != nil && h.selfUpdate.AckSender != nil && ack != nil {
		_ = h.selfUpdate.AckSender(ack)
		return nil
	}
	return ack
}

// divergedSelfUpdate marks the ack DIVERGED and records the failure in the
// applied log. The entry is deliberately NOT CONVERGED, so alreadyApplied still
// returns false and a re-issued self_update gets a real retry.
func (h *Handler) divergedSelfUpdate(ack *amxv1.CommandAck, code string, err error) *amxv1.CommandAck {
	out := diverged(ack, code, err)
	h.record(out, "self_update", "", code)
	return out
}

// commitMatches compares a commit against the pin, accepting an abbreviated SHA
// as long as it is a prefix of at least 7 hex characters — the git default, and
// the floor the REST schema enforces. A shorter pin is treated as no match
// rather than as a loose one.
func commitMatches(head, want string) bool {
	if len(want) < 7 || len(want) > len(head) {
		return false
	}
	return strings.EqualFold(head[:len(want)], want)
}

func shortCommit(c string) string {
	if len(c) > 12 {
		return c[:12]
	}
	return c
}

// tail keeps the last 1 KiB of build output; a full compiler log would blow past
// what an ack detail should carry, and the error is at the end.
func tail(s string) string {
	s = strings.TrimSpace(s)
	if len(s) <= 1024 {
		return s
	}
	return "..." + s[len(s)-1024:]
}

// OSSelfUpdateRunner is the production SelfUpdateRunner: real processes, real
// filesystem, real exec. FreeBytes and Exec are platform-specific and live in
// selfupdate_unix.go / selfupdate_other.go.
type OSSelfUpdateRunner struct{}

func (OSSelfUpdateRunner) LookPath(file string) (string, error) { return exec.LookPath(file) }

func (OSSelfUpdateRunner) Run(ctx context.Context, spec RunSpec) (string, error) {
	if spec.Timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, spec.Timeout)
		defer cancel()
	}
	c := exec.CommandContext(ctx, spec.Name, spec.Args...)
	c.Dir = spec.Dir
	if spec.Env != nil {
		c.Env = spec.Env
	}
	// git이 띄운 손자(ssh, git-remote-https)가 stdout 파이프를 물고 있으면
	// 자식 kill 후에도 CombinedOutput이 반환하지 않는다 — 파이프를 강제로
	// 닫아 타임아웃이 실제로 반환까지 보장되게 한다.
	c.WaitDelay = 5 * time.Second
	out, err := c.CombinedOutput()
	// CommandContext kills the child on deadline, surfacing as a generic "signal:
	// killed". Re-label it so the ack says which step hung.
	if err != nil && ctx.Err() == context.DeadlineExceeded {
		return string(out), fmt.Errorf("%w after %s: %v", ErrRunTimeout, spec.Timeout, err)
	}
	return string(out), err
}

func (OSSelfUpdateRunner) Rename(oldpath, newpath string) error {
	return os.Rename(oldpath, newpath)
}

func (OSSelfUpdateRunner) Remove(name string) error {
	if err := os.Remove(name); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}
