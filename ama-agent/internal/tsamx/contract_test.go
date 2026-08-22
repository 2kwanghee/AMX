// 이 파일은 엔진 교체 시 양쪽(tsamx ExecBridge / 향후 native Bridge)이 통과해야
// 하는 계약을 고정한다: 시트 엔진 재설계 기획서(docs/design-notes/seat-engine-plan.md
// P0-A)가 조사로 확정한 8개 계약 중 CLI 동사·인자·환경변수(계약1), auto --once 종료
// 코드(계약2), list --json 스키마 v1(계약3), 리터럴 의미(계약4), 격리 상태 파일
// 경로·형식(계약6), 미지 필드 무시(계약8)를 exec.go(ExecBridge)에 대해 고정한다.
// PoolSummary 계산(계약5)은 internal/reporter/contract_test.go, 동기 완료(계약7)는
// 이미 fake_test.go의 TestFakeLifecycle이 다루는 범위를 이 파일 하단 주석에서
// 짚어준다.
//
// 실제 tsamx 바이너리를 실행하지 않는다: ExecBridge.Binary/BaseEnv/ConfigDir/
// DataHome이 이미 공개 필드라 프로덕션 코드를 바꾸지 않고도 테스트 전용 스텁
// 스크립트(POSIX sh)를 그 자리에 꽂을 수 있다. 스텁은 각 호출의 argv를 파일에
// 기록하고, 지정된 종료코드로 종료하며, 지정된 골든 픽스처를 stdout으로 그대로
// 흘려보낸다.
package tsamx

import (
	"context"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"testing"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
)

// --- 스텁 실행자 -------------------------------------------------------------

// stubScript is a POSIX sh program shared by every contract test in this file.
// It never touches a real tsamx install:
//   - appends this invocation's argv (args joined by \x1f, one call per line) to
//     $STUB_ARGV_FILE, so a test can assert both argv AND call order/count;
//   - if $STUB_ENV_FILE is set, appends the lines of its own environment that
//     match AMX_STUB_/XDG_DATA_HOME= (the env contract under test), so a test can
//     assert what ExecBridge.env() injected without dumping the whole environment;
//   - if $STUB_STDOUT_FILE is set, cats that fixture verbatim to stdout;
//   - exits with $STUB_EXIT_CODE (default 0).
const stubScript = `#!/bin/sh
{
  first=1
  for a in "$@"; do
    if [ "$first" -eq 0 ]; then printf '\037'; fi
    printf '%s' "$a"
    first=0
  done
  printf '\n'
} >> "$STUB_ARGV_FILE"
if [ -n "$STUB_ENV_FILE" ]; then
  env | grep -E '^(AMX_STUB_|XDG_DATA_HOME=)' >> "$STUB_ENV_FILE"
  printf -- '---\n' >> "$STUB_ENV_FILE"
fi
if [ -n "$STUB_STDOUT_FILE" ]; then
  cat "$STUB_STDOUT_FILE"
fi
exit "${STUB_EXIT_CODE:-0}"
`

// writeStub installs stubScript into dir and returns its path.
func writeStub(t *testing.T, dir string) string {
	t.Helper()
	path := filepath.Join(dir, "stub-tsamx.sh")
	if err := os.WriteFile(path, []byte(stubScript), 0o755); err != nil {
		t.Fatalf("write stub script: %v", err)
	}
	return path
}

// stubDriver is a minimal provider.Driver for the Add() contract path, which
// needs a Driver to stage the credential and to contribute Env(). It is
// test-only and lives in this _test.go file (no production code changes).
type stubDriver struct{}

func (stubDriver) Name() string                          { return "stub" }
func (stubDriver) ConfigHome() string                    { return "" }
func (stubDriver) CredentialPath(configDir string) string { return filepath.Join(configDir, "cred.json") }
func (stubDriver) StageCredential(configDir string, credentialJSON []byte, meta provider.AddMeta) error {
	return nil
}
func (stubDriver) Fingerprint(credentialJSON []byte) string { return "" }
func (stubDriver) HasCredentialMaterial(credentialJSON []byte) bool {
	return len(credentialJSON) > 0
}
func (stubDriver) DefaultConfigHome() string { return "" }
func (stubDriver) Env(configDir string) []string {
	return []string{"AMX_STUB_CONFIG_DIR=" + configDir}
}
func (stubDriver) BinaryName() string { return "tsamx" }

// Identity: stub, unused by any golden contract assertion in this file — added
// only so stubDriver keeps satisfying provider.Driver after P3 (design note)
// added the method to the interface. Not part of the fixed contract this file
// pins.
func (stubDriver) Identity(configDir string) (string, error) { return "", nil }

var _ provider.Driver = stubDriver{}

// stubBridge returns an ExecBridge wired to a fresh stub script: exitCode is
// what every invocation exits with, and stdoutFixture (a path under testdata/,
// or "" for none) is what every invocation prints to stdout. It returns the
// bridge and the path argv calls are recorded to.
func stubBridge(t *testing.T, exitCode int, stdoutFixture string) (*ExecBridge, string) {
	t.Helper()
	dir := t.TempDir()
	bin := writeStub(t, dir)
	argvFile := filepath.Join(dir, "argv.log")
	env := append([]string(nil), os.Environ()...)
	env = append(env, "STUB_ARGV_FILE="+argvFile, "STUB_EXIT_CODE="+strconv.Itoa(exitCode))
	if stdoutFixture != "" {
		env = append(env, "STUB_STDOUT_FILE="+stdoutFixture)
	}
	b := &ExecBridge{
		Binary:    bin,
		BaseEnv:   env,
		ConfigDir: filepath.Join(dir, "config-home"),
		Driver:    stubDriver{},
	}
	return b, argvFile
}

// readCalls parses argvFile into one []string per recorded invocation, in call
// order.
func readCalls(t *testing.T, argvFile string) [][]string {
	t.Helper()
	blob, err := os.ReadFile(argvFile)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		t.Fatalf("read argv log: %v", err)
	}
	text := strings.TrimRight(string(blob), "\n")
	if text == "" {
		return nil
	}
	lines := strings.Split(text, "\n")
	out := make([][]string, 0, len(lines))
	for _, l := range lines {
		out = append(out, strings.Split(l, "\x1f"))
	}
	return out
}

// noOpStdout is a fixture every argv-only test can use: it is valid list --json
// v1 output, so a case that happens to call List() (or any verb that merely
// ignores stdout) never fails on JSON parsing.
const noOpStdout = "testdata/list_v1_null_active.json"

// --- 계약1: CLI 동사·인자 -----------------------------------------------------

// TestContractCLIVerbArgv locks the exact argv ExecBridge execs for each pool
// verb (계약1). A native engine's compat CLI (or the CLI-facing side of the
// ExecBridge equivalent) must accept the same argv shape.
func TestContractCLIVerbArgv(t *testing.T) {
	cases := []struct {
		tag  string
		call func(b *ExecBridge) error
		want []string
	}{
		{"C1_remove_yes_flag", func(b *ExecBridge) error {
			return b.Remove(context.Background(), "a@x.io")
		}, []string{"remove", "a@x.io", "--yes"}},
		{"C1_enable", func(b *ExecBridge) error {
			return b.Enable(context.Background(), "a@x.io")
		}, []string{"enable", "a@x.io"}},
		{"C1_disable", func(b *ExecBridge) error {
			return b.Disable(context.Background(), "a@x.io")
		}, []string{"disable", "a@x.io"}},
		{"C1_switch_target", func(b *ExecBridge) error {
			return b.Switch(context.Background(), "a@x.io")
		}, []string{"switch", "a@x.io"}},
		{"C1_switch_strategy_best", func(b *ExecBridge) error {
			return b.SwitchStrategy(context.Background(), "best")
		}, []string{"switch", "--strategy", "best"}},
		{"C1_switch_strategy_next_available", func(b *ExecBridge) error {
			return b.SwitchStrategy(context.Background(), "next-available")
		}, []string{"switch", "--strategy", "next-available"}},
		{"C1_list_json", func(b *ExecBridge) error {
			_, err := b.List(context.Background())
			return err
		}, []string{"list", "--json"}},
	}
	for _, c := range cases {
		t.Run(c.tag, func(t *testing.T) {
			b, argvFile := stubBridge(t, 0, noOpStdout)
			if err := c.call(b); err != nil {
				t.Fatalf("call: %v", err)
			}
			calls := readCalls(t, argvFile)
			if len(calls) != 1 {
				t.Fatalf("recorded %d calls, want 1: %v", len(calls), calls)
			}
			if !reflect.DeepEqual(calls[0], c.want) {
				t.Fatalf("argv = %v, want %v", calls[0], c.want)
			}
		})
	}
}

// TestContractAddArgv locks Add's two shapes (계약1): a plain add (no argument,
// exec.go stages the credential via the Driver first) and add-then-disable when
// the caller wants the new slot to start out of rotation (Enable: false).
func TestContractAddArgv(t *testing.T) {
	t.Run("C1_add_enabled_single_call", func(t *testing.T) {
		b, argvFile := stubBridge(t, 0, noOpStdout)
		if err := b.Add(context.Background(), provider.AddRequest{Email: "a@x.io", Enable: true}); err != nil {
			t.Fatalf("Add: %v", err)
		}
		calls := readCalls(t, argvFile)
		want := [][]string{{"add"}}
		if !reflect.DeepEqual(calls, want) {
			t.Fatalf("calls = %v, want %v", calls, want)
		}
	})
	t.Run("C1_add_disabled_follow_up_disable", func(t *testing.T) {
		b, argvFile := stubBridge(t, 0, noOpStdout)
		if err := b.Add(context.Background(), provider.AddRequest{Email: "b@x.io", Enable: false}); err != nil {
			t.Fatalf("Add: %v", err)
		}
		calls := readCalls(t, argvFile)
		want := [][]string{{"add"}, {"disable", "b@x.io"}}
		if !reflect.DeepEqual(calls, want) {
			t.Fatalf("calls = %v, want %v", calls, want)
		}
	})
}

// TestContractConfigSetNumberFormat locks the Go strconv 'g' formatting of
// numeric config values (계약1: "Go strconv 'g' 포맷"). A native engine must
// format the same way if it shells out to the same compat CLI, or must parse
// this exact shape if it IS the compat CLI's replacement.
func TestContractConfigSetNumberFormat(t *testing.T) {
	cases := []struct {
		tag   string
		call  func(b *ExecBridge) error
		value float64
		want  []string
	}{
		{"C1_threshold_whole_number", func(b *ExecBridge) error {
			return b.ConfigSetThreshold(context.Background(), 90)
		}, 90, []string{"config", "set", "autoswitch.threshold", "90"}},
		{"C1_threshold_fraction", func(b *ExecBridge) error {
			return b.ConfigSetThreshold(context.Background(), 92.5)
		}, 92.5, []string{"config", "set", "autoswitch.threshold", "92.5"}},
		{"C1_config_cooldown_seconds", func(b *ExecBridge) error {
			return b.ConfigSet(context.Background(), "cooldownSeconds", 120)
		}, 120, []string{"config", "set", "autoswitch.cooldownSeconds", "120"}},
		{"C1_config_hysteresis_small_fraction", func(b *ExecBridge) error {
			return b.ConfigSet(context.Background(), "hysteresisPct", 0.001)
		}, 0.001, []string{"config", "set", "autoswitch.hysteresisPct", "0.001"}},
	}
	for _, c := range cases {
		t.Run(c.tag, func(t *testing.T) {
			b, argvFile := stubBridge(t, 0, noOpStdout)
			if err := c.call(b); err != nil {
				t.Fatalf("call: %v", err)
			}
			calls := readCalls(t, argvFile)
			if len(calls) != 1 || !reflect.DeepEqual(calls[0], c.want) {
				t.Fatalf("argv = %v, want [%v]", calls, c.want)
			}
		})
	}
}

// TestContractEnvInjection locks the environment ExecBridge.env() builds
// (계약1's implicit "config home" carrier): the provider Driver's Env(configDir)
// entries and XDG_DATA_HOME must reach the child process, since a runner wrapper
// and (per the feasibility doc) the agent itself both depend on the same config
// home resolving to the same directory.
func TestContractEnvInjection(t *testing.T) {
	dir := t.TempDir()
	bin := writeStub(t, dir)
	argvFile := filepath.Join(dir, "argv.log")
	envFile := filepath.Join(dir, "env.log")
	configDir := filepath.Join(dir, "config-home")
	dataHome := filepath.Join(dir, "data-home")
	env := append([]string(nil), os.Environ()...)
	env = append(env, "STUB_ARGV_FILE="+argvFile, "STUB_ENV_FILE="+envFile)

	b := &ExecBridge{Binary: bin, BaseEnv: env, ConfigDir: configDir, DataHome: dataHome, Driver: stubDriver{}}
	if err := b.Enable(context.Background(), "a@x.io"); err != nil {
		t.Fatalf("Enable: %v", err)
	}

	blob, err := os.ReadFile(envFile)
	if err != nil {
		t.Fatalf("read env log: %v", err)
	}
	got := string(blob)
	if want := "AMX_STUB_CONFIG_DIR=" + configDir; !strings.Contains(got, want) {
		t.Fatalf("env missing %q, got:\n%s", want, got)
	}
	if want := "XDG_DATA_HOME=" + dataHome; !strings.Contains(got, want) {
		t.Fatalf("env missing %q, got:\n%s", want, got)
	}
}

// --- 계약2: auto --once 종료코드 --------------------------------------------

// TestContractAutoOnceExitCodes locks the exit-code contract (계약2): 0 switched,
// 1 error, 2 no action, 3 blocked/all-exhausted. The scheduler maps 0 to
// KIND_SWITCH and 3 to KIND_ALL_EXHAUSTED (internal/scheduler/scheduler_test.go
// TestTickActiveChangeEnqueuesSwitch / TestTickAllExhaustedByCode already lock
// that mapping against the Fake; this test locks the ExecBridge side: that the
// real exit code, not some derived signal, drives it).
func TestContractAutoOnceExitCodes(t *testing.T) {
	cases := []struct {
		tag      string
		exitCode int
		wantCode int
		wantErr  bool
	}{
		{"C2_exit0_switched", 0, 0, false},
		{"C2_exit1_error", 1, 1, true},
		{"C2_exit2_no_action", 2, 2, false},
		{"C2_exit3_blocked_all_exhausted", 3, 3, false},
	}
	for _, c := range cases {
		t.Run(c.tag, func(t *testing.T) {
			b, _ := stubBridge(t, c.exitCode, "")
			code, err := b.AutoOnce(context.Background())
			if code != c.wantCode {
				t.Fatalf("code = %d, want %d", code, c.wantCode)
			}
			if (err != nil) != c.wantErr {
				t.Fatalf("err = %v, wantErr = %v", err, c.wantErr)
			}
		})
	}
}

// --- 계약3/4/8: list --json 스키마 v1, 리터럴, 미지 필드 무시 -----------------

// TestContractListSchemaV1Basic locks the fields AMA actually reads off
// list --json (계약3) plus the FiveHour/SevenDay -> Windows dual-record
// projection, and that a per-account "disabled" key absent from the JSON
// defaults to false (계약3 "disabled(부재=false)") rather than failing to parse.
func TestContractListSchemaV1Basic(t *testing.T) {
	abs, err := filepath.Abs("testdata/list_v1_basic.json")
	if err != nil {
		t.Fatal(err)
	}
	b, _ := stubBridge(t, 0, abs)
	res, err := b.List(context.Background())
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if res.SchemaVersion != 1 {
		t.Fatalf("schemaVersion = %d, want 1", res.SchemaVersion)
	}
	if res.ActiveAccountNumber == nil || *res.ActiveAccountNumber != 1 {
		t.Fatalf("activeAccountNumber = %v, want 1", res.ActiveAccountNumber)
	}
	if len(res.Accounts) != 2 {
		t.Fatalf("accounts = %d, want 2", len(res.Accounts))
	}
	a, b2 := res.Accounts[0], res.Accounts[1]

	if a.Number != 1 || a.Email != "a@x.io" || a.OrganizationName != "Acme" || a.OrganizationUUID != "uuid-a" {
		t.Fatalf("account a identity = %+v", a)
	}
	if !a.Active || a.Disabled {
		t.Fatalf("account a active/disabled = %v/%v, want true/false", a.Active, a.Disabled)
	}
	if a.UsageStatus != "ok" || a.Alias != "primary" || a.UsageFetchedAt != "2026-08-23T09:59:00Z" {
		t.Fatalf("account a status/alias/fetchedAt = %+v", a)
	}
	if a.Usage == nil || a.Usage.FiveHour == nil || a.Usage.FiveHour.Pct != 61.2 {
		t.Fatalf("account a usage.fiveHour = %+v", a.Usage)
	}
	if a.Usage.SevenDay == nil || a.Usage.SevenDay.Pct != 44 {
		t.Fatalf("account a usage.sevenDay = %+v", a.Usage.SevenDay)
	}
	if a.Usage.Spend == nil || a.Usage.Spend.Pct != 12.3 || a.Usage.Spend.Currency != "USD" {
		t.Fatalf("account a usage.spend = %+v", a.Usage.Spend)
	}
	if len(a.Usage.Scoped) != 1 || a.Usage.Scoped[0].Name != "claude-3-opus" {
		t.Fatalf("account a usage.scoped = %+v", a.Usage.Scoped)
	}
	// Windows dual-record: five_hour/300 then seven_day/10080, values mirroring
	// the positional fields (ExecBridge.List's claudeWindows projection).
	if len(a.Usage.Windows) != 2 {
		t.Fatalf("account a usage.windows = %+v, want 2 entries", a.Usage.Windows)
	}
	if a.Usage.Windows[0].Id != "five_hour" || a.Usage.Windows[0].WindowMinutes != 300 || a.Usage.Windows[0].Pct != 61.2 {
		t.Fatalf("windows[0] = %+v", a.Usage.Windows[0])
	}
	if a.Usage.Windows[1].Id != "seven_day" || a.Usage.Windows[1].WindowMinutes != 10080 || a.Usage.Windows[1].Pct != 44 {
		t.Fatalf("windows[1] = %+v", a.Usage.Windows[1])
	}

	// account b omits "disabled" and "usageFetchedAt" entirely in the fixture:
	// both must default to their Go zero value (false / ""), not fail to parse.
	if b2.Disabled {
		t.Fatalf("account b disabled = true, want false (key absent from JSON, 계약3)")
	}
	if b2.UsageFetchedAt != "" {
		t.Fatalf("account b usageFetchedAt = %q, want empty (key absent from JSON)", b2.UsageFetchedAt)
	}
}

// TestContractListLiterals locks the usageStatus/usage/disabled literal meanings
// (계약4): "relogin_required" is a quarantine signal with usage=null (not merely
// "usage happens to be absent"), and disabled=true is orthogonal to usageStatus
// (a disabled account can still report "ok" while carrying usage=null, mirroring
// a real disabled+never-fetched slot).
func TestContractListLiterals(t *testing.T) {
	abs, err := filepath.Abs("testdata/list_v1_relogin_and_disabled.json")
	if err != nil {
		t.Fatal(err)
	}
	b, _ := stubBridge(t, 0, abs)
	res, err := b.List(context.Background())
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	byEmail := map[string]provider.AccountRow{}
	for _, row := range res.Accounts {
		byEmail[row.Email] = row
	}

	dead := byEmail["dead@x.io"]
	if dead.UsageStatus != "relogin_required" {
		t.Fatalf("dead usageStatus = %q, want relogin_required", dead.UsageStatus)
	}
	if dead.Usage != nil {
		t.Fatalf("dead usage = %+v, want nil (unmeasured, 계약4)", dead.Usage)
	}
	if dead.Disabled {
		t.Fatalf("dead disabled = true, want false (quarantine and disabled are independent signals)")
	}

	off := byEmail["off@x.io"]
	if !off.Disabled {
		t.Fatalf("off disabled = false, want true")
	}
	if off.Usage != nil {
		t.Fatalf("off usage = %+v, want nil", off.Usage)
	}
	if off.UsageStatus != "ok" {
		t.Fatalf("off usageStatus = %q, want ok (disabled != quarantined literal)", off.UsageStatus)
	}
}

// TestContractListNullActiveAccountNumber locks the nullable
// activeAccountNumber (계약3): an empty pool (or one with no active account)
// must parse to a nil pointer, not a zero-valued *int.
func TestContractListNullActiveAccountNumber(t *testing.T) {
	abs, err := filepath.Abs("testdata/list_v1_null_active.json")
	if err != nil {
		t.Fatal(err)
	}
	b, _ := stubBridge(t, 0, abs)
	res, err := b.List(context.Background())
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if res.ActiveAccountNumber != nil {
		t.Fatalf("activeAccountNumber = %v, want nil", *res.ActiveAccountNumber)
	}
	if len(res.Accounts) != 0 {
		t.Fatalf("accounts = %d, want 0", len(res.Accounts))
	}
}

// TestContractListUnknownFieldsIgnored locks 계약8: a future tsamx (or a native
// engine's compat CLI) may add fields AMA does not model yet — at the top
// level, per-account, and inside the usage object — and parsing must still
// succeed and populate every field AMA DOES read.
func TestContractListUnknownFieldsIgnored(t *testing.T) {
	abs, err := filepath.Abs("testdata/list_v1_unknown_fields.json")
	if err != nil {
		t.Fatal(err)
	}
	b, _ := stubBridge(t, 0, abs)
	res, err := b.List(context.Background())
	if err != nil {
		t.Fatalf("List with unknown fields must not fail to parse: %v", err)
	}
	if len(res.Accounts) != 1 {
		t.Fatalf("accounts = %d, want 1", len(res.Accounts))
	}
	row := res.Accounts[0]
	if row.Email != "z@x.io" || !row.Active || row.Alias != "z" {
		t.Fatalf("known fields not populated despite unknown siblings: %+v", row)
	}
	if row.Usage == nil || row.Usage.FiveHour == nil || row.Usage.FiveHour.Pct != 5 {
		t.Fatalf("usage.fiveHour not populated despite unknown usage.pace/countdown: %+v", row.Usage)
	}
}

// TestContractStatusDerivesFromList locks that ExecBridge.Status projects the
// SAME active account List reports (exec.go comment: tsamx emits no stable
// top-level status schema narrow enough to trust, so Status is derived from
// List rather than a separate `tsamx status --json` parse).
func TestContractStatusDerivesFromList(t *testing.T) {
	abs, err := filepath.Abs("testdata/list_v1_basic.json")
	if err != nil {
		t.Fatal(err)
	}
	b, _ := stubBridge(t, 0, abs)
	st, err := b.Status(context.Background())
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	if st.ActiveAccountNumber == nil || *st.ActiveAccountNumber != 1 {
		t.Fatalf("ActiveAccountNumber = %v, want 1", st.ActiveAccountNumber)
	}
	if st.ActiveEmail != "a@x.io" {
		t.Fatalf("ActiveEmail = %q, want a@x.io", st.ActiveEmail)
	}
}

// --- 계약6: 격리 상태 파일 -----------------------------------------------------

// TestContractAutoStatePathLinux locks the Linux/WSL state-file path convention
// (계약6: $XDG_DATA_HOME/tsamx/autoswitch_state.json). The Windows convention
// (~/.tsamx-backup) is gated on runtime.GOOS at RUN time, not a build tag, so it
// cannot be exercised from a process actually running on Linux — see the report
// for why that half of 계약6 is not fixed here.
func TestContractAutoStatePathLinux(t *testing.T) {
	dataHome := t.TempDir()
	b := &ExecBridge{DataHome: dataHome}
	want := filepath.Join(dataHome, "tsamx", "autoswitch_state.json")
	if got := b.AutoStatePath(); got != want {
		t.Fatalf("AutoStatePath() = %q, want %q", got, want)
	}
}

// TestContractReadQuarantineParsesAtomicRenameWrite locks the quarantine state
// file's JSON shape (계약6: {"quarantine":{"<slot>":{"email":...}}}) and that
// ReadQuarantine reads it correctly when the file arrived via tsamx's actual
// write pattern — write to a temp path, then atomically rename into place —
// which is what lets the scheduler's fsnotify watch treat a rename as the
// change signal.
func TestContractReadQuarantineParsesAtomicRenameWrite(t *testing.T) {
	dataHome := t.TempDir()
	b := &ExecBridge{DataHome: dataHome}
	stateDir := filepath.Join(dataHome, "tsamx")
	if err := os.MkdirAll(stateDir, 0o755); err != nil {
		t.Fatal(err)
	}
	final := filepath.Join(stateDir, "autoswitch_state.json")
	tmp := final + ".tmp"
	payload := `{"quarantine":{"2":{"email":"dead@x.io"},"5":{"email":"other@x.io"}},"unknownTopLevelKey":123}`
	if err := os.WriteFile(tmp, []byte(payload), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(tmp, final); err != nil {
		t.Fatal(err)
	}

	got, err := b.ReadQuarantine(context.Background())
	if err != nil {
		t.Fatalf("ReadQuarantine: %v", err)
	}
	want := map[string]string{"2": "dead@x.io", "5": "other@x.io"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("quarantine = %v, want %v", got, want)
	}
}

// TestContractReadQuarantineMissingFileIsEmpty locks the "nothing quarantined
// yet" case: no autoswitch_state.json at all must yield an empty map and a nil
// error, never a hard failure (a fresh pool with no auto-switch history has no
// state file).
func TestContractReadQuarantineMissingFileIsEmpty(t *testing.T) {
	b := &ExecBridge{DataHome: t.TempDir()}
	got, err := b.ReadQuarantine(context.Background())
	if err != nil {
		t.Fatalf("ReadQuarantine: %v", err)
	}
	if len(got) != 0 {
		t.Fatalf("quarantine = %v, want empty", got)
	}
}

// --- 계약7 (참고): 동기 완료 ----------------------------------------------------
//
// "switch/add/enable 반환 시점에 이어지는 List/Status가 새 active를 반환한다"는
// 계약은 실제 tsamx 바이너리의 성질이며, 이 파일의 스텁(테스트가 프로그램한 그대로
// 반환하는 스크립트)으로는 검증할 수 없다 — 스텁은 항상 테스트가 원하는 답을
// 돌려주므로 "통과"가 아무것도 증명하지 못한다. 이 계약이 실제로 성립하는지는
// fake.go의 Fake(모든 소비자가 실제로 링크하는 in-memory Bridge)가 만족하는지로만
// 고정할 수 있고, 그 부분은 fake_test.go의 TestFakeLifecycle이 이미 커버한다
// (Add→Add→Switch→Status, Remove→List 각각에서 직후 읽기가 새 상태를 반환하는지
// 확인한다). 실제 tsamx CLI 자체의 동기성은 e2e 스위트의 몫이다.
