package main

import (
	"testing"

	"github.com/2kwanghee/AMX/ama-agent/internal/provider"
	"github.com/2kwanghee/AMX/ama-agent/internal/provider/codex"
)

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
