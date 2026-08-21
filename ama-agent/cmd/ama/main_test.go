package main

import (
	"testing"
	"time"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
	"github.com/2kwanghee/AMX/ama-agent/internal/provider/codex"
	amxv1 "github.com/2kwanghee/AMX/contracts/gen/go"
)

// TestNewHeartbeatCarriesCachedStatus (defect 4): a heartbeat always stamps
// sent_at, and carries active_account/tsamx_healthy from the cached report status
// once a report has run — without spawning tsamx. Before the first report
// (have=false) those two fields are omitted rather than reported as a false
// "unhealthy, no active account".
func TestNewHeartbeatCarriesCachedStatus(t *testing.T) {
	sentAt := time.Unix(1700000000, 0)

	// Before the first report: sent_at set, status fields omitted.
	hb := newHeartbeat("ama_test", amxv1.SwitchMode_SWITCH_MODE_AUTO, 3, heartbeatStatus{}, sentAt)
	if hb.GetSentAt() == nil || !hb.GetSentAt().AsTime().Equal(sentAt.UTC()) {
		t.Fatalf("sent_at = %v, want %v", hb.GetSentAt().AsTime(), sentAt.UTC())
	}
	if hb.GetAgentId() != "ama_test" || hb.GetSwitchMode() != amxv1.SwitchMode_SWITCH_MODE_AUTO || hb.GetOutboxDepth() != 3 {
		t.Fatalf("base fields wrong: %+v", hb)
	}
	if hb.GetActiveAccount() != nil || hb.GetTsamxHealthy() {
		t.Fatalf("status must be omitted before first report, got active=%v healthy=%v",
			hb.GetActiveAccount(), hb.GetTsamxHealthy())
	}

	// After a healthy report: active_account + tsamx_healthy carried through.
	status := heartbeatStatus{
		activeAccount: &amxv1.AccountRef{Email: "a@x.io", Provider: provider.DefaultProvider},
		tsamxHealthy:  true,
		have:          true,
	}
	hb = newHeartbeat("ama_test", amxv1.SwitchMode_SWITCH_MODE_AUTO, 0, status, sentAt)
	if hb.GetActiveAccount().GetEmail() != "a@x.io" {
		t.Fatalf("active_account = %+v, want a@x.io", hb.GetActiveAccount())
	}
	if !hb.GetTsamxHealthy() {
		t.Fatal("tsamx_healthy should be true after a healthy report")
	}

	// After a failed report tick: have=true but healthy=false.
	status.tsamxHealthy = false
	hb = newHeartbeat("ama_test", amxv1.SwitchMode_SWITCH_MODE_AUTO, 0, status, sentAt)
	if hb.GetTsamxHealthy() {
		t.Fatal("tsamx_healthy should be false when the last report tick failed")
	}
}

// TestMaybeRegisterCodexGate: the codex provider is wired into the registry only
// when AMX_CODEX_HOME is set. Unset leaves the map claude-only and returns nil, so
// the agent's pre-multi-provider behavior is unchanged.
func TestMaybeRegisterCodexGate(t *testing.T) {
	drv := codex.New()

	// Unset: no bridge registered, nil returned.
	t.Setenv(codex.EnvConfigHome, "")
	bridges := map[string]provider.Bridge{provider.DefaultProvider: nil}
	if got := maybeRegisterCodex(bridges, drv); got != nil {
		t.Fatalf("codex bridge returned with AMX_CODEX_HOME unset: %v", got)
	}
	if _, ok := bridges["codex"]; ok {
		t.Fatal("codex must not be registered with AMX_CODEX_HOME unset")
	}

	// Set: bridge registered under the codex key and returned.
	t.Setenv(codex.EnvConfigHome, t.TempDir())
	if got := maybeRegisterCodex(bridges, drv); got == nil {
		t.Fatal("codex bridge not returned with AMX_CODEX_HOME set")
	}
	if _, ok := bridges["codex"]; !ok {
		t.Fatal("codex must be registered under its key with AMX_CODEX_HOME set")
	}
}
