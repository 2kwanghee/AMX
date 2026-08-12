// Package tsamx bridges the AMA daemon to the tsamx CLI (design note §6, SSOT
// §6.3). It implements the vendor-neutral provider.Bridge control surface: the
// Bridge interface and its DTOs live in internal/provider, and ExecBridge here is
// the tsamx-backed implementation. All effects go through provider.Bridge so
// tests use Fake and never exec the real CLI. ExecBridge runs against the agent's
// own provider config home / XDG_DATA_HOME (the config home is resolved by the
// injected provider.Driver) and parses `--json` output.
package tsamx
