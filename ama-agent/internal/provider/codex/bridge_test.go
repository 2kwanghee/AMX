package codex

import (
	"context"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
)

func newTestBridge(t *testing.T) *Bridge {
	t.Helper()
	return &Bridge{Driver: New(), ConfigDir: t.TempDir(), LockMaxWait: 50 * time.Millisecond}
}

// TestLifecycle walks Add -> List(active) -> Disable -> List(inactive) -> Enable
// -> Remove and checks the on-disk state and 0600 perms at each step.
func TestLifecycle(t *testing.T) {
	b := newTestBridge(t)
	ctx := context.Background()
	dir := b.ConfigDir

	if err := b.Add(ctx, provider.AddRequest{Email: "u@x", CredentialJSON: []byte(authV1), Enable: true}); err != nil {
		t.Fatalf("add: %v", err)
	}
	// auth.json and meta both present and 0600.
	for _, f := range []string{"auth.json", metaFile} {
		fi, err := os.Stat(filepath.Join(dir, f))
		if err != nil {
			t.Fatalf("stat %s: %v", f, err)
		}
		if perm := fi.Mode().Perm(); perm != 0o600 {
			t.Fatalf("%s perm = %o, want 600", f, perm)
		}
	}

	list, err := b.List(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(list.Accounts) != 1 || !list.Accounts[0].Active || list.Accounts[0].Disabled {
		t.Fatalf("after add: want one active account, got %+v", list.Accounts)
	}
	if list.Accounts[0].Email != "u@x" {
		t.Fatalf("email = %q, want u@x (from meta sidecar)", list.Accounts[0].Email)
	}
	if list.ActiveAccountNumber == nil || *list.ActiveAccountNumber != 1 {
		t.Fatalf("activeAccountNumber = %v, want 1", list.ActiveAccountNumber)
	}

	if err := b.Disable(ctx, "u@x"); err != nil {
		t.Fatalf("disable: %v", err)
	}
	if fileExists(b.authPath(dir)) || !fileExists(b.disabledPath(dir)) {
		t.Fatal("disable must rename auth.json to the disabled form")
	}
	list, _ = b.List(ctx)
	if list.Accounts[0].Active || !list.Accounts[0].Disabled {
		t.Fatalf("after disable: want inactive+disabled, got %+v", list.Accounts[0])
	}
	if list.ActiveAccountNumber != nil {
		t.Fatal("after disable: activeAccountNumber must be nil")
	}
	// Idempotent: a second disable succeeds as a no-op.
	if err := b.Disable(ctx, "u@x"); err != nil {
		t.Fatalf("second disable must be a no-op success: %v", err)
	}

	if err := b.Enable(ctx, "u@x"); err != nil {
		t.Fatalf("enable: %v", err)
	}
	if !fileExists(b.authPath(dir)) || fileExists(b.disabledPath(dir)) {
		t.Fatal("enable must restore auth.json")
	}

	if err := b.Remove(ctx, "u@x"); err != nil {
		t.Fatalf("remove: %v", err)
	}
	list, _ = b.List(ctx)
	if len(list.Accounts) != 0 {
		t.Fatalf("after remove: want no accounts, got %+v", list.Accounts)
	}
	// Idempotent: removing again succeeds.
	if err := b.Remove(ctx, "u@x"); err != nil {
		t.Fatalf("second remove must be a no-op success: %v", err)
	}
}

// TestAddDisabled verifies Enable=false parks the account (disabled immediately).
func TestAddDisabled(t *testing.T) {
	b := newTestBridge(t)
	if err := b.Add(context.Background(), provider.AddRequest{Email: "u@x", CredentialJSON: []byte(authV1), Enable: false}); err != nil {
		t.Fatal(err)
	}
	if fileExists(b.authPath(b.ConfigDir)) {
		t.Fatal("Add with Enable=false must leave auth.json disabled")
	}
	if !fileExists(b.disabledPath(b.ConfigDir)) {
		t.Fatal("disabled credential must be retained")
	}
}

// TestNoopVerbs pins the surface Codex does not support.
func TestNoopVerbs(t *testing.T) {
	b := newTestBridge(t)
	ctx := context.Background()
	if err := b.Switch(ctx, "x"); err != nil {
		t.Fatal(err)
	}
	if err := b.SwitchStrategy(ctx, "best"); err != nil {
		t.Fatal(err)
	}
	if err := b.ConfigSet(ctx, "cooldown_seconds", 1); err != nil {
		t.Fatal(err)
	}
	if err := b.ConfigSetThreshold(ctx, 80); err != nil {
		t.Fatal(err)
	}
	if code, err := b.AutoOnce(ctx); err != nil || code != autoOnceNoAction {
		t.Fatalf("AutoOnce = (%d, %v), want (2, nil)", code, err)
	}
	if b.AutoStatePath() != "" {
		t.Fatal("AutoStatePath must be empty")
	}
	q, err := b.ReadQuarantine(ctx)
	if err != nil || len(q) != 0 {
		t.Fatalf("ReadQuarantine = (%v, %v), want (empty, nil)", q, err)
	}
}

func writeRollout(t *testing.T, dir, rel, body string) {
	t.Helper()
	full := filepath.Join(dir, "sessions", rel)
	if err := os.MkdirAll(filepath.Dir(full), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(full, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

// TestUsagePrimaryOnly parses a rollout whose last token_count has only primary,
// and confirms it lands in Windows (not the Claude-shaped FiveHour/SevenDay).
func TestUsagePrimaryOnly(t *testing.T) {
	b := newTestBridge(t)
	body := `{"type":"event_msg","payload":{"type":"other"}}
{"type":"event_msg","payload":{"type":"token_count","rate_limits":{"primary":{"used_percent":42.5,"window_minutes":300,"resets_at":1000000000},"secondary":null}}}
`
	writeRollout(t, b.ConfigDir, "2026/08/12/rollout-abc.jsonl", body)
	u := b.readUsage(b.ConfigDir)
	if u == nil || len(u.Windows) != 1 {
		t.Fatalf("want one window, got %+v", u)
	}
	if u.FiveHour != nil || u.SevenDay != nil {
		t.Fatalf("Codex must not populate Claude-shaped fields, got %+v/%+v", u.FiveHour, u.SevenDay)
	}
	w := u.Windows[0]
	if w.Id != "primary" || w.Pct != 42.5 || w.WindowMinutes != 300 {
		t.Fatalf("window = %+v, want primary/42.5/300", w)
	}
	if w.ResetsAt != time.Unix(1000000000, 0).UTC().Format(time.RFC3339) {
		t.Fatalf("resetsAt = %q", w.ResetsAt)
	}
}

// TestUsageWithSecondary parses a rollout with both windows and confirms the last
// token_count wins over an earlier one.
func TestUsageWithSecondary(t *testing.T) {
	b := newTestBridge(t)
	body := `{"type":"event_msg","payload":{"type":"token_count","rate_limits":{"primary":{"used_percent":10,"resets_at":1},"secondary":null}}}
{"type":"event_msg","payload":{"type":"token_count","rate_limits":{"primary":{"used_percent":55,"window_minutes":300,"resets_at":2000000000},"secondary":{"used_percent":88,"window_minutes":10080,"resets_at":2000600000}}}}
`
	writeRollout(t, b.ConfigDir, "2026/08/12/rollout-def.jsonl", body)
	u := b.readUsage(b.ConfigDir)
	if u == nil || len(u.Windows) != 2 {
		t.Fatalf("want two windows, got %+v", u)
	}
	if u.Windows[0].Id != "primary" || u.Windows[0].Pct != 55 {
		t.Fatalf("primary = %+v, want last event 55", u.Windows[0])
	}
	if u.Windows[1].Id != "secondary" || u.Windows[1].Pct != 88 || u.Windows[1].WindowMinutes != 10080 {
		t.Fatalf("secondary = %+v, want 88/10080", u.Windows[1])
	}
}

// TestUsageNoFile returns nil usage when no rollout exists.
func TestUsageNoFile(t *testing.T) {
	b := newTestBridge(t)
	if u := b.readUsage(b.ConfigDir); u != nil {
		t.Fatalf("no rollout must yield nil usage, got %+v", u)
	}
}

// TestUsageLatestFileWins picks the most recently modified rollout across days.
func TestUsageLatestFileWins(t *testing.T) {
	b := newTestBridge(t)
	old := `{"type":"event_msg","payload":{"type":"token_count","rate_limits":{"primary":{"used_percent":1,"resets_at":1},"secondary":null}}}` + "\n"
	newer := `{"type":"event_msg","payload":{"type":"token_count","rate_limits":{"primary":{"used_percent":99,"resets_at":2},"secondary":null}}}` + "\n"
	writeRollout(t, b.ConfigDir, "2026/08/11/rollout-old.jsonl", old)
	writeRollout(t, b.ConfigDir, "2026/08/12/rollout-new.jsonl", newer)
	oldPath := filepath.Join(b.ConfigDir, "sessions/2026/08/11/rollout-old.jsonl")
	newPath := filepath.Join(b.ConfigDir, "sessions/2026/08/12/rollout-new.jsonl")
	past := time.Now().Add(-time.Hour)
	now := time.Now()
	_ = os.Chtimes(oldPath, past, past)
	_ = os.Chtimes(newPath, now, now)
	u := b.readUsage(b.ConfigDir)
	if u == nil || len(u.Windows) != 1 || u.Windows[0].Pct != 99 {
		t.Fatalf("want latest file's 99, got %+v", u)
	}
}

// TestListExcludesUnmanaged verifies a hand-logged-in home (auth.json, no meta)
// is NOT reported as a managed account (B-H2).
func TestListExcludesUnmanaged(t *testing.T) {
	b := newTestBridge(t)
	if err := os.WriteFile(b.authPath(b.ConfigDir), []byte(authV1), 0o600); err != nil {
		t.Fatal(err)
	}
	list, err := b.List(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(list.Accounts) != 0 {
		t.Fatalf("unmanaged home must report no accounts, got %+v", list.Accounts)
	}
}

// TestAddDifferentEmailRejected verifies a second, different account is refused
// while re-adding the same email (credential refresh) is allowed (B-M2).
func TestAddDifferentEmailRejected(t *testing.T) {
	b := newTestBridge(t)
	ctx := context.Background()
	if err := b.Add(ctx, provider.AddRequest{Email: "a@x", CredentialJSON: []byte(authV1), Enable: true}); err != nil {
		t.Fatal(err)
	}
	err := b.Add(ctx, provider.AddRequest{Email: "b@x", CredentialJSON: []byte(authV2), Enable: true})
	if err == nil {
		t.Fatal("adding a different email must be rejected (single-account)")
	}
	// Same email again is a refresh: allowed, and the new credential is staged.
	if err := b.Add(ctx, provider.AddRequest{Email: "a@x", CredentialJSON: []byte(authV2), Enable: true}); err != nil {
		t.Fatalf("re-adding same email (refresh) must succeed: %v", err)
	}
	got, _ := os.ReadFile(b.authPath(b.ConfigDir))
	if string(got) != authV2 {
		t.Fatal("refresh must overwrite auth.json with the new credential")
	}
}

// TestAddClearsStaleDisabled verifies re-delivering an account as enabled clears
// its prior disabled credential so Enable cannot resurrect it (B-H3).
func TestAddClearsStaleDisabled(t *testing.T) {
	b := newTestBridge(t)
	ctx := context.Background()
	if err := b.Add(ctx, provider.AddRequest{Email: "a@x", CredentialJSON: []byte(authV1), Enable: false}); err != nil {
		t.Fatal(err)
	}
	if !fileExists(b.disabledPath(b.ConfigDir)) {
		t.Fatal("precondition: expected a disabled credential")
	}
	if err := b.Add(ctx, provider.AddRequest{Email: "a@x", CredentialJSON: []byte(authV2), Enable: true}); err != nil {
		t.Fatal(err)
	}
	if fileExists(b.disabledPath(b.ConfigDir)) {
		t.Fatal("stale disabled credential must be cleared on re-delivery")
	}
	if !fileExists(b.authPath(b.ConfigDir)) {
		t.Fatal("new delivery must be active")
	}
}

// TestEnableRefusesOverwrite verifies Enable never clobbers a live auth.json when
// a disabled credential is also present (B-H3).
func TestEnableRefusesOverwrite(t *testing.T) {
	b := newTestBridge(t)
	dir := b.ConfigDir
	if err := b.Add(context.Background(), provider.AddRequest{Email: "a@x", CredentialJSON: []byte(authV1), Enable: true}); err != nil {
		t.Fatal(err)
	}
	// Manufacture the ambiguous state: both live and disabled credentials present.
	if err := os.WriteFile(b.disabledPath(dir), []byte(authV2), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := b.Enable(context.Background(), "a@x"); err == nil {
		t.Fatal("Enable must refuse to overwrite a live credential")
	}
	got, _ := os.ReadFile(b.authPath(dir))
	if string(got) != authV1 {
		t.Fatal("live credential must be left intact")
	}
}

// TestMismatchedAccountNoop verifies verbs targeting a different email leave the
// installed account untouched (B-M1).
func TestMismatchedAccountNoop(t *testing.T) {
	b := newTestBridge(t)
	ctx := context.Background()
	if err := b.Add(ctx, provider.AddRequest{Email: "a@x", CredentialJSON: []byte(authV1), Enable: true}); err != nil {
		t.Fatal(err)
	}
	if err := b.Disable(ctx, "other@x"); err != nil {
		t.Fatalf("mismatched disable must be a no-op success: %v", err)
	}
	if !fileExists(b.authPath(b.ConfigDir)) {
		t.Fatal("mismatched disable must not touch the installed account")
	}
	if err := b.Remove(ctx, "other@x"); err != nil {
		t.Fatalf("mismatched remove must be a no-op success: %v", err)
	}
	if !fileExists(b.authPath(b.ConfigDir)) || !fileExists(b.metaPath(b.ConfigDir)) {
		t.Fatal("mismatched remove must not delete the installed account")
	}
}

// TestConfigDirOverrideMismatch verifies a per-call override may only name the
// bridge's own config home (A-1).
func TestConfigDirOverrideMismatch(t *testing.T) {
	b := newTestBridge(t)
	ctx := context.Background()
	err := b.Add(ctx, provider.AddRequest{Email: "a@x", CredentialJSON: []byte(authV1), Enable: true, ConfigDir: t.TempDir()})
	if err == nil {
		t.Fatal("a divergent ConfigDir override must error")
	}
	// A matching override is accepted.
	if err := b.Add(ctx, provider.AddRequest{Email: "a@x", CredentialJSON: []byte(authV1), Enable: true, ConfigDir: b.ConfigDir}); err != nil {
		t.Fatalf("matching override must be accepted: %v", err)
	}
}

// TestDeliverLockExclusive verifies a second acquisition fails open (bounded
// retry) while the first is held, and succeeds once released.
func TestDeliverLockExclusive(t *testing.T) {
	b := newTestBridge(t)
	ctx := context.Background()

	release := b.DeliverLock(ctx)
	if release == nil {
		t.Fatal("DeliverLock must always return a non-nil release")
	}
	// Independently probe the same lock file with a non-blocking flock: it must
	// currently be held (EWOULDBLOCK).
	lockPath := filepath.Join(b.ConfigDir, deliverLockName)
	f, err := os.OpenFile(lockPath, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != syscall.EWOULDBLOCK {
		t.Fatalf("lock should be held: flock err = %v, want EWOULDBLOCK", err)
	}
	if err := release(); err != nil {
		t.Fatalf("release: %v", err)
	}
	// After release the probe can take it.
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		t.Fatalf("after release the lock must be free: %v", err)
	}
	_ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
}
