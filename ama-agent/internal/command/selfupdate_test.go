package command

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
	"google.golang.org/protobuf/proto"
)

// fakeRunner is a programmable SelfUpdateRunner. Commands whose "name arg0 arg1"
// prefix is registered in fail/out are answered from the maps; anything else is
// forwarded to pass (a real OSSelfUpdateRunner) when set, so a test can drive
// real git against a temp repo while keeping `go build` and exec fake.
// Rename/Remove always hit the real filesystem — the binary swap is the part
// worth exercising for real — and Exec is recorded instead of performed, since a
// real syscall.Exec would replace the test process.
type fakeRunner struct {
	pass SelfUpdateRunner

	lookPathErr error
	free        uint64
	freeErr     error
	out         map[string]string
	fail        map[string]error
	// onRun fires the moment a matching command is invoked, so a fake `go build`
	// can produce its artifact exactly where the real one would.
	onRun map[string]func()

	calls    []string
	specs    []RunSpec
	execArgv string
	execErr  error
}

func newFakeRunner() *fakeRunner {
	return &fakeRunner{
		free:  DefaultSelfUpdateMinFreeBytes * 2,
		out:   map[string]string{},
		fail:  map[string]error{},
		onRun: map[string]func(){},
	}
}

func (f *fakeRunner) LookPath(string) (string, error) {
	if f.lookPathErr != nil {
		return "", f.lookPathErr
	}
	return "/usr/bin/go", nil
}

func (f *fakeRunner) FreeBytes(string) (uint64, error) { return f.free, f.freeErr }

// key collapses a command to the prefix tests match on: the binary's base name
// plus its first two arguments ("git pull --ff-only", "go build -ldflags").
func key(name string, args []string) string {
	parts := append([]string{filepath.Base(name)}, args...)
	if len(parts) > 3 {
		parts = parts[:3]
	}
	return strings.Join(parts, " ")
}

func (f *fakeRunner) Run(ctx context.Context, spec RunSpec) (string, error) {
	k := key(spec.Name, spec.Args)
	f.calls = append(f.calls, k)
	f.specs = append(f.specs, spec)
	if hook, ok := f.onRun[k]; ok {
		hook()
	}
	if err, ok := f.fail[k]; ok {
		return f.out[k], err
	}
	if out, ok := f.out[k]; ok {
		return out, nil
	}
	if f.pass != nil {
		return f.pass.Run(ctx, spec)
	}
	return "", nil
}

func (f *fakeRunner) Rename(o, n string) error { return os.Rename(o, n) }

func (f *fakeRunner) Remove(n string) error {
	if err := os.Remove(n); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

func (f *fakeRunner) Exec(argv0 string, _, _ []string) error {
	f.execArgv = argv0
	return f.execErr
}

func (f *fakeRunner) ran(k string) bool {
	for _, c := range f.calls {
		if c == k {
			return true
		}
	}
	return false
}

func (f *fakeRunner) specFor(t *testing.T, k string) RunSpec {
	t.Helper()
	for i, c := range f.calls {
		if c == k {
			return f.specs[i]
		}
	}
	t.Fatalf("%q was never run (ran %v)", k, f.calls)
	return RunSpec{}
}

// testCommit is the fake upstream tip / HEAD the stubbed git reports, and the
// string the stubbed `--version` prints back.
const testCommit = "d4c3b2a1908f7e6d5c4b3a29180f7e6d5c4b3a29"

// selfUpdateEnv installs a SelfUpdateConfig on the harness pointing at a temp
// repo dir and a placeholder "installed binary", and stubs a healthy git + build
// + smoke sequence. Individual tests override the step they are about to break.
func selfUpdateEnv(t *testing.T, hn *harness, f *fakeRunner) (repoDir, binPath string) {
	t.Helper()
	repoDir = t.TempDir()
	binPath = filepath.Join(t.TempDir(), "ama")
	if err := os.WriteFile(binPath, []byte("OLD-BINARY"), 0o755); err != nil {
		t.Fatal(err)
	}
	hn.h.selfUpdate = &SelfUpdateConfig{
		RepoDir:    repoDir,
		ModuleDir:  repoDir,
		BinaryPath: binPath,
		Runner:     f,
	}
	f.out["git rev-parse @{u}"] = testCommit
	f.out["git rev-parse HEAD"] = testCommit
	f.out["ama.new --version"] = "p3+" + shortCommit(testCommit) + "\n"
	f.onRun["go build -ldflags"] = func() {
		if err := os.WriteFile(binPath+newBinarySuffix, []byte("NEW-BINARY"), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	return repoDir, binPath
}

func selfUpdateCmd(t *testing.T, hn *harness, cmdID, expectedCommit string) *amxv1.AmsCommand {
	t.Helper()
	return hn.sign(t, &amxv1.AmsCommand{
		CommandId: cmdID,
		Cmd: &amxv1.AmsCommand_SelfUpdate{SelfUpdate: &amxv1.SelfUpdate{
			ExpectedCommit: expectedCommit,
		}},
	})
}

func TestSelfUpdateUnconfiguredIsRejected(t *testing.T) {
	hn := newHarness(t)
	ack := hn.apply(t, selfUpdateCmd(t, hn, "su-none", ""))
	if ack.GetConvergence() != amxv1.CommandAck_CONVERGENCE_REJECTED || ack.GetErrorCode() != "unsupported_command" {
		t.Fatalf("want REJECTED/unsupported_command, got %v/%q", ack.GetConvergence(), ack.GetErrorCode())
	}
}

func TestSelfUpdatePreflightFailureLeavesBinaryUntouched(t *testing.T) {
	for _, tc := range []struct {
		name  string
		setup func(*fakeRunner)
	}{
		{"no go toolchain", func(f *fakeRunner) { f.lookPathErr = errors.New("exec: \"go\": not found") }},
		{"disk full", func(f *fakeRunner) { f.free = 1 << 20 }},
		{"statfs error", func(f *fakeRunner) { f.freeErr = errors.New("statfs: permission denied") }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			hn := newHarness(t)
			f := newFakeRunner()
			tc.setup(f)
			_, binPath := selfUpdateEnv(t, hn, f)

			ack := hn.apply(t, selfUpdateCmd(t, hn, "su-pre", ""))
			if ack.GetConvergence() != amxv1.CommandAck_CONVERGENCE_DIVERGED || ack.GetErrorCode() != "preflight_failed" {
				t.Fatalf("want DIVERGED/preflight_failed, got %v/%q (%s)", ack.GetConvergence(), ack.GetErrorCode(), ack.GetDetail())
			}
			// The whole point of the preflight: nothing was fetched, built or swapped.
			if len(f.calls) != 0 {
				t.Fatalf("preflight failure still ran %v", f.calls)
			}
			if b, _ := os.ReadFile(binPath); string(b) != "OLD-BINARY" {
				t.Fatalf("installed binary changed: %q", b)
			}
			if f.execArgv != "" {
				t.Fatal("exec was called after a preflight failure")
			}
			// A DIVERGED entry must not satisfy the idempotency gate, so a retry works.
			if hn.h.alreadyApplied("su-pre") {
				t.Fatal("a failed self_update was recorded as already applied")
			}
		})
	}
}

// TestSelfUpdatePinIsCheckedBeforeTheTreeMoves is the ordering guarantee: the pin
// is a veto on what the agent is about to become, so a mismatch must be decided
// against the fetched upstream tip while the working tree is still untouched.
// Checking it after the pull would leave the operator's clone already advanced to
// a commit they explicitly refused.
func TestSelfUpdatePinIsCheckedBeforeTheTreeMoves(t *testing.T) {
	hn := newHarness(t)
	f := newFakeRunner()
	f.pass = OSSelfUpdateRunner{} // real git for fetch/rev-parse
	repo, head, _ := initRepo(t)
	_, binPath := selfUpdateEnv(t, hn, f)
	hn.h.selfUpdate.RepoDir = repo
	hn.h.selfUpdate.ModuleDir = repo
	delete(f.out, "git rev-parse @{u}") // let real git answer
	delete(f.out, "git rev-parse HEAD")

	ack := hn.apply(t, selfUpdateCmd(t, hn, "su-pin", strings.Repeat("a", 40)))
	if ack.GetConvergence() != amxv1.CommandAck_CONVERGENCE_DIVERGED || ack.GetErrorCode() != "commit_mismatch" {
		t.Fatalf("want DIVERGED/commit_mismatch, got %v/%q (%s)", ack.GetConvergence(), ack.GetErrorCode(), ack.GetDetail())
	}
	if !strings.Contains(ack.GetDetail(), head) {
		t.Fatalf("detail should name the real upstream tip %s: %s", head, ack.GetDetail())
	}
	// The decisive assertions: the pull never ran, so the tree never moved.
	if f.ran("git pull --ff-only") {
		t.Fatal("git pull ran despite a pin that could not be satisfied")
	}
	if f.ran("go build -ldflags") {
		t.Fatal("build ran despite a commit mismatch")
	}
	if b, _ := os.ReadFile(binPath); string(b) != "OLD-BINARY" {
		t.Fatalf("installed binary changed: %q", b)
	}

	// The real upstream tip is accepted, in full and abbreviated form.
	for _, pin := range []string{head, head[:7], strings.ToUpper(head[:10])} {
		if !commitMatches(head, pin) {
			t.Fatalf("pin %q should match %s", pin, head)
		}
	}
	// Too short to be meaningful, and an unrelated sha, are both rejected.
	if commitMatches(head, head[:6]) || commitMatches(head, strings.Repeat("b", 40)) {
		t.Fatal("commitMatches accepted a pin it should not")
	}
}

// initRepo builds a real git clone with an upstream, so the fetch/pull/rev-parse
// path is exercised against real git rather than a stub.
func initRepo(t *testing.T) (clone string, headSHA string, origin string) {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not available")
	}
	base := t.TempDir()
	origin = filepath.Join(base, "origin")
	clone = filepath.Join(base, "clone")
	git := func(dir string, args ...string) string {
		c := exec.Command("git", args...)
		c.Dir = dir
		c.Env = append(os.Environ(),
			"GIT_AUTHOR_NAME=t", "GIT_AUTHOR_EMAIL=t@t", "GIT_COMMITTER_NAME=t",
			"GIT_COMMITTER_EMAIL=t@t", "GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_SYSTEM=/dev/null")
		out, err := c.CombinedOutput()
		if err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, out)
		}
		return strings.TrimSpace(string(out))
	}
	if err := os.MkdirAll(origin, 0o755); err != nil {
		t.Fatal(err)
	}
	git(origin, "init", "-q", "-b", "main")
	if err := os.WriteFile(filepath.Join(origin, "f.txt"), []byte("v1"), 0o644); err != nil {
		t.Fatal(err)
	}
	git(origin, "add", ".")
	git(origin, "commit", "-qm", "v1")
	git(base, "clone", "-q", origin, clone)
	git(clone, "config", "user.email", "t@t")
	git(clone, "config", "user.name", "t")
	return clone, git(clone, "rev-parse", "HEAD"), origin
}

func TestSelfUpdateGitPullFailure(t *testing.T) {
	hn := newHarness(t)
	f := newFakeRunner()
	f.pass = OSSelfUpdateRunner{}
	repo, _, origin := initRepo(t)
	_, binPath := selfUpdateEnv(t, hn, f)
	hn.h.selfUpdate.RepoDir = repo
	hn.h.selfUpdate.ModuleDir = repo
	delete(f.out, "git rev-parse @{u}")
	delete(f.out, "git rev-parse HEAD")

	// Diverge the two histories so `git pull --ff-only` genuinely refuses: a new
	// commit upstream AND a different local commit.
	git := func(dir string, args ...string) {
		c := exec.Command("git", args...)
		c.Dir = dir
		c.Env = append(os.Environ(), "GIT_AUTHOR_NAME=t", "GIT_AUTHOR_EMAIL=t@t",
			"GIT_COMMITTER_NAME=t", "GIT_COMMITTER_EMAIL=t@t")
		if out, err := c.CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, out)
		}
	}
	if err := os.WriteFile(filepath.Join(origin, "f.txt"), []byte("v2"), 0o644); err != nil {
		t.Fatal(err)
	}
	git(origin, "commit", "-aqm", "v2")
	if err := os.WriteFile(filepath.Join(repo, "local.txt"), []byte("local"), 0o644); err != nil {
		t.Fatal(err)
	}
	git(repo, "add", ".")
	git(repo, "commit", "-qm", "local")

	ack := hn.apply(t, selfUpdateCmd(t, hn, "su-pull", ""))
	if ack.GetConvergence() != amxv1.CommandAck_CONVERGENCE_DIVERGED || ack.GetErrorCode() != "git_pull_failed" {
		t.Fatalf("want DIVERGED/git_pull_failed, got %v/%q (%s)", ack.GetConvergence(), ack.GetErrorCode(), ack.GetDetail())
	}
	if f.ran("go build -ldflags") {
		t.Fatal("build ran despite a failed pull")
	}
	if b, _ := os.ReadFile(binPath); string(b) != "OLD-BINARY" {
		t.Fatalf("installed binary changed: %q", b)
	}
}

func TestSelfUpdateBuildAndSmokeFailuresCleanUp(t *testing.T) {
	for _, tc := range []struct {
		name string
		k    string
		code string
	}{
		{"build", "go build -ldflags", "build_failed"},
		{"smoke", "ama.new --version", "smoke_failed"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			hn := newHarness(t)
			f := newFakeRunner()
			_, binPath := selfUpdateEnv(t, hn, f)
			f.fail[tc.k] = errors.New("boom")
			f.out[tc.k] = "compiler said no"

			ack := hn.apply(t, selfUpdateCmd(t, hn, "su-b", ""))
			if ack.GetConvergence() != amxv1.CommandAck_CONVERGENCE_DIVERGED || ack.GetErrorCode() != tc.code {
				t.Fatalf("want DIVERGED/%s, got %v/%q (%s)", tc.code, ack.GetConvergence(), ack.GetErrorCode(), ack.GetDetail())
			}
			if !strings.Contains(ack.GetDetail(), "compiler said no") {
				t.Fatalf("detail should carry the tool output: %s", ack.GetDetail())
			}
			if _, err := os.Stat(binPath + newBinarySuffix); !os.IsNotExist(err) {
				t.Fatal("ama.new was left behind after a failure")
			}
			if b, _ := os.ReadFile(binPath); string(b) != "OLD-BINARY" {
				t.Fatalf("installed binary changed: %q", b)
			}
			if f.execArgv != "" {
				t.Fatal("exec was called after a failed build")
			}
		})
	}
}

// TestSelfUpdateSmokeRejectsABinaryThatDoesNotKnowTheFlag covers the case the
// smoke test exists for: a commit predating --version treats the flag as noise
// and starts up as a full agent. Its output never names the built commit, so the
// update is refused rather than installing a binary that would register a second
// live session and could never be self-updated again.
func TestSelfUpdateSmokeRejectsABinaryThatDoesNotKnowTheFlag(t *testing.T) {
	hn := newHarness(t)
	f := newFakeRunner()
	_, binPath := selfUpdateEnv(t, hn, f)
	f.out["ama.new --version"] = "2026/08/12 ama: connecting to AMS...\n"

	ack := hn.apply(t, selfUpdateCmd(t, hn, "su-smoke", ""))
	if ack.GetConvergence() != amxv1.CommandAck_CONVERGENCE_DIVERGED || ack.GetErrorCode() != "smoke_failed" {
		t.Fatalf("want DIVERGED/smoke_failed, got %v/%q (%s)", ack.GetConvergence(), ack.GetErrorCode(), ack.GetDetail())
	}
	if !strings.Contains(ack.GetDetail(), shortCommit(testCommit)) {
		t.Fatalf("detail should name the commit that was expected: %s", ack.GetDetail())
	}
	if _, err := os.Stat(binPath + newBinarySuffix); !os.IsNotExist(err) {
		t.Fatal("ama.new was left behind")
	}
	if b, _ := os.ReadFile(binPath); string(b) != "OLD-BINARY" {
		t.Fatalf("installed binary changed: %q", b)
	}
	if f.execArgv != "" {
		t.Fatal("a binary that failed its smoke test was started anyway")
	}
}

// The smoke run gets a whitelist, not the agent's environment: nothing in it can
// let the child find AMS and register as a duplicate agent.
func TestSelfUpdateSmokeRunsWithAMinimalEnvironment(t *testing.T) {
	t.Setenv("AMX_AGENT_ID", "ama_prod")
	t.Setenv("AMX_AMS_ADDR", "ams.example:50051")
	t.Setenv("AMX_ENROLL_TOKEN", "tok")
	hn := newHarness(t)
	f := newFakeRunner()
	selfUpdateEnv(t, hn, f)

	hn.apply(t, selfUpdateCmd(t, hn, "su-env", ""))
	spec := f.specFor(t, "ama.new --version")
	if spec.Env == nil {
		t.Fatal("smoke inherited the agent's environment")
	}
	for _, kv := range spec.Env {
		if k, _, _ := strings.Cut(kv, "="); k != "PATH" && k != "HOME" {
			t.Fatalf("smoke environment carries %q; only PATH and HOME are allowed", k)
		}
	}
}

// Every step is bounded, so a hung git or a wedged linker acks timeout_<step>
// instead of parking the command forever.
func TestSelfUpdateStepsAreBounded(t *testing.T) {
	for _, tc := range []struct {
		name, k, code string
		want          time.Duration
	}{
		{"git", "git fetch --quiet", "timeout_git", gitStepTimeout},
		{"build", "go build -ldflags", "timeout_build", buildStepTimeout},
		{"smoke", "ama.new --version", "timeout_smoke", smokeStepTimeout},
	} {
		t.Run(tc.name, func(t *testing.T) {
			hn := newHarness(t)
			f := newFakeRunner()
			_, binPath := selfUpdateEnv(t, hn, f)
			f.fail[tc.k] = errors.New("signal: killed")

			// The declared bound reaches the runner.
			hn.apply(t, selfUpdateCmd(t, hn, "su-t0", ""))
			if got := f.specFor(t, tc.k).Timeout; got != tc.want {
				t.Fatalf("%s timeout = %s, want %s", tc.k, got, tc.want)
			}

			// A deadline kill is reported as timeout_<step>, not as the step's
			// ordinary failure code.
			f.fail[tc.k] = fmt.Errorf("%w after %s: signal: killed", ErrRunTimeout, tc.want)
			ack := hn.apply(t, selfUpdateCmd(t, hn, "su-t1", ""))
			if ack.GetConvergence() != amxv1.CommandAck_CONVERGENCE_DIVERGED || ack.GetErrorCode() != tc.code {
				t.Fatalf("want DIVERGED/%s, got %v/%q (%s)", tc.code, ack.GetConvergence(), ack.GetErrorCode(), ack.GetDetail())
			}
			if b, _ := os.ReadFile(binPath); string(b) != "OLD-BINARY" {
				t.Fatalf("installed binary changed: %q", b)
			}
			if f.execArgv != "" {
				t.Fatal("exec ran after a timeout")
			}
		})
	}
}

// TestSelfUpdateSwapsRecordsAndRestarts covers the committed half of the
// sequence. Note what it does NOT claim: the fake AckSender resolves inline, so
// this proves the ack is HANDED to the transport before exec, not that it
// reached AMS. Delivery is best-effort by design — the durable applied-log entry
// asserted below is what actually answers a re-queue.
func TestSelfUpdateSwapsRecordsAndRestarts(t *testing.T) {
	hn := newHarness(t)
	f := newFakeRunner()
	_, binPath := selfUpdateEnv(t, hn, f)

	var handedToTransport []*amxv1.CommandAck
	hn.h.selfUpdate.AckSender = func(a *amxv1.CommandAck) error {
		if f.execArgv != "" {
			t.Error("ack was handed to the transport after exec")
		}
		handedToTransport = append(handedToTransport, a)
		return nil
	}

	ack := hn.apply(t, selfUpdateCmd(t, hn, "su-ok", testCommit[:7]))
	if ack != nil {
		t.Fatalf("Handle must return nil once the AckSender took the ack, got %v", ack)
	}
	if len(handedToTransport) != 1 || handedToTransport[0].GetConvergence() != amxv1.CommandAck_CONVERGENCE_CONVERGED {
		t.Fatalf("want one CONVERGED ack, got %v", handedToTransport)
	}
	if f.execArgv != binPath {
		t.Fatalf("exec argv0 = %q, want %q", f.execArgv, binPath)
	}
	if b, _ := os.ReadFile(binPath); string(b) != "NEW-BINARY" {
		t.Fatalf("new binary was not installed: %q", b)
	}
	if b, _ := os.ReadFile(binPath + backupSuffix); string(b) != "OLD-BINARY" {
		t.Fatalf("ama.bak does not hold the previous binary: %q", b)
	}
	// Durable before exec: the restarted process answers a re-queue from this.
	entry, seen := hn.appl.Lookup("su-ok")
	if !seen || entry.Convergence != amxv1.CommandAck_CONVERGENCE_CONVERGED.String() || entry.Target != testCommit {
		t.Fatalf("applied log entry = %+v (seen=%v)", entry, seen)
	}
}

// A lost ack must not be repaired by re-acking: the applied log already says
// CONVERGED, and AMS learns it from the re-queue. Asserted here because the
// tempting fix — retract and re-send — would defeat the idempotency gate.
func TestSelfUpdateAckSendFailureStillConverges(t *testing.T) {
	hn := newHarness(t)
	f := newFakeRunner()
	_, binPath := selfUpdateEnv(t, hn, f)
	var logged []string
	hn.h.selfUpdate.Logf = func(format string, args ...any) {
		logged = append(logged, fmt.Sprintf(format, args...))
	}
	hn.h.selfUpdate.AckSender = func(*amxv1.CommandAck) error { return errors.New("stream closed") }

	if got := hn.apply(t, selfUpdateCmd(t, hn, "su-lost", "")); got != nil {
		t.Fatalf("Handle should return nil after handing the ack over, got %v", got)
	}
	entry, seen := hn.appl.Lookup("su-lost")
	if !seen || entry.Convergence != amxv1.CommandAck_CONVERGENCE_CONVERGED.String() {
		t.Fatalf("applied log entry = %+v (seen=%v); a lost ack must not undo the record", entry, seen)
	}
	if f.execArgv != binPath {
		t.Fatal("a failed ack send must not stop the restart")
	}
	if len(logged) == 0 {
		t.Fatal("a lost ack should be logged — nothing else carries the news")
	}
}

// A failed exec is NOT a failed update. The swap happened and is durably
// recorded CONVERGED; only the restart did not. Retracting that with a DIVERGED
// re-ack would tell AMS the update failed while the new binary sits installed,
// and would un-suppress the re-queue the applied log exists to absorb.
func TestSelfUpdateExecFailureKeepsTheConvergedRecord(t *testing.T) {
	hn := newHarness(t)
	f := newFakeRunner()
	_, binPath := selfUpdateEnv(t, hn, f)
	f.execErr = errors.New("exec format error")
	var acks []*amxv1.CommandAck
	var logged []string
	hn.h.selfUpdate.Logf = func(format string, args ...any) {
		logged = append(logged, fmt.Sprintf(format, args...))
	}
	hn.h.selfUpdate.AckSender = func(a *amxv1.CommandAck) error {
		acks = append(acks, proto.Clone(a).(*amxv1.CommandAck))
		return nil
	}

	if got := hn.apply(t, selfUpdateCmd(t, hn, "su-exec", "")); got != nil {
		t.Fatalf("Handle returned an ack after one was already sent: %v", got)
	}
	if len(acks) != 1 {
		t.Fatalf("want exactly one ack, got %d: %v", len(acks), acks)
	}
	if acks[0].GetConvergence() != amxv1.CommandAck_CONVERGENCE_CONVERGED {
		t.Fatalf("the single ack should stay CONVERGED, got %v/%q", acks[0].GetConvergence(), acks[0].GetErrorCode())
	}
	entry, _ := hn.appl.Lookup("su-exec")
	if entry.Convergence != amxv1.CommandAck_CONVERGENCE_CONVERGED.String() {
		t.Fatalf("applied log was rewritten to %q", entry.Convergence)
	}
	if b, _ := os.ReadFile(binPath); string(b) != "NEW-BINARY" {
		t.Fatalf("the new binary should stay installed for the next restart: %q", b)
	}
	if len(logged) == 0 || !strings.Contains(strings.Join(logged, "\n"), "exec failed") {
		t.Fatalf("the failed restart should be logged: %v", logged)
	}
}

func TestSelfUpdateRequeueIsIdempotent(t *testing.T) {
	hn := newHarness(t)
	f := newFakeRunner()
	_, binPath := selfUpdateEnv(t, hn, f)

	var sent []*amxv1.CommandAck
	hn.h.selfUpdate.AckSender = func(a *amxv1.CommandAck) error { sent = append(sent, a); return nil }
	if got := hn.apply(t, selfUpdateCmd(t, hn, "su-dup", "")); got != nil {
		t.Fatalf("first pass should have sent its own ack, got %v", got)
	}
	callsAfterFirst := len(f.calls)
	f.execArgv = ""

	// AMS re-queues the same command_id (the ack was lost, or the agent restarted
	// into the new binary before it landed). This is the path the restarted
	// process takes, so it must not rebuild or restart again.
	if got := hn.apply(t, selfUpdateCmd(t, hn, "su-dup", "")); got != nil {
		t.Fatalf("replay should have sent its own ack, got %v", got)
	}
	if len(sent) != 2 || sent[1].GetConvergence() != amxv1.CommandAck_CONVERGENCE_CONVERGED {
		t.Fatalf("replay ack = %v, want a second CONVERGED", sent)
	}
	if len(f.calls) != callsAfterFirst {
		t.Fatalf("replay re-ran %v", f.calls[callsAfterFirst:])
	}
	if f.execArgv != "" {
		t.Fatal("replay restarted the agent a second time")
	}
	if b, _ := os.ReadFile(binPath); string(b) != "NEW-BINARY" {
		t.Fatalf("replay disturbed the installed binary: %q", b)
	}
}

// TestUnknownOneofIsRejected pins the compatibility contract an agent built
// against an OLDER proto relies on: a command whose oneof field number this
// build does not know parses into the unknown-field set, GetCmd() is nil, and
// the dispatcher nacks REJECTED/unknown_command instead of doing anything.
// Field 19 stands in for "self_update = 18 seen by a pre-self_update agent" —
// it is the same code path (command.go dispatch default).
func TestUnknownOneofIsRejected(t *testing.T) {
	hn := newHarness(t)
	// Wire bytes for field 19, wire type 2 (length-delimited), empty submessage:
	// tag = 19<<3|2 = 154, 1 -> varint 0x9a 0x01, then length 0.
	raw := []byte{0x9a, 0x01, 0x00}
	cmd := &amxv1.AmsCommand{CommandId: "su-old"}
	if err := proto.Unmarshal(raw, cmd); err != nil {
		t.Fatal(err)
	}
	cmd.CommandId = "su-old"
	if cmd.GetCmd() != nil {
		t.Fatal("field 19 should be unknown to this build")
	}
	ack := hn.apply(t, hn.sign(t, cmd))
	if ack.GetConvergence() != amxv1.CommandAck_CONVERGENCE_REJECTED || ack.GetErrorCode() != "unknown_command" {
		t.Fatalf("want REJECTED/unknown_command, got %v/%q (%s)", ack.GetConvergence(), ack.GetErrorCode(), ack.GetDetail())
	}
}
