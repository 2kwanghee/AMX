package tsamx

import (
	"bytes"
	"context"
	"os/exec"
	"strings"
	"sync"
	"time"
)

// EngineVersion reports which seat engine (and version) this agent's tsamx
// bridge is running, as "<engine>/<version>" for Register.tsamx_version
// (design note docs/design-notes/seat-engine-plan.md, P0-B). Today the only
// engine is tsamx itself (the native bridge lands in P6), so the prefix is
// always "tsamx".
//
// It shells out to `<binary> --version` at most once per process: Register is
// re-sent on every reconnect (cmd/ama/main.go OnConnect), and the answer cannot
// change while this process keeps running, so a package-level sync.Once caches
// it. This assumes a single relevant binary per process — true today, since
// only the claude driver's tsamx binary is reported.
//
// Both failure outcomes are fail-open (Register is never blocked):
//   - "tsamx/absent": the binary is not on PATH, or it ran but failed/exited
//     non-zero. The binary itself is the problem, and that fact is the useful
//     diagnostic.
//   - "tsamx/unknown": the binary ran fine but its --version output did not
//     parse into a recognizable version token.
func EngineVersion(ctx context.Context, binary string) string {
	versionOnce.Do(func() {
		versionCached = queryEngineVersion(ctx, binary)
	})
	return versionCached
}

var (
	versionOnce   sync.Once
	versionCached string
)

// versionQueryTimeout bounds the single `--version` invocation queryEngineVersion
// ever makes (memoized by EngineVersion, so this cost is paid at most once).
const versionQueryTimeout = 5 * time.Second

func queryEngineVersion(ctx context.Context, binary string) string {
	if binary == "" {
		binary = "tsamx"
	}
	if _, err := exec.LookPath(binary); err != nil {
		return "tsamx/absent"
	}
	qctx, cancel := context.WithTimeout(ctx, versionQueryTimeout)
	defer cancel()
	cmd := exec.CommandContext(qctx, binary, "--version")
	var out bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &out
	if err := cmd.Run(); err != nil {
		return "tsamx/absent"
	}
	v := parseVersionToken(out.String())
	if v == "" {
		return "tsamx/unknown"
	}
	return "tsamx/" + v
}

// parseVersionToken extracts the version from `tsamx --version` output.
// argparse's version action renders "%(prog)s <version>" (tsamx/src/tsamx/
// cli.py: `version=f"%(prog)s {__version__}"`) — confirmed against the
// installed binary: `tsamx --version` prints "tsamx 0.25.0b1". The prog name
// can vary (an installed shim, or the "cswap" alias tsamx's own bug-report
// template mentions), so the LAST whitespace-separated field is taken rather
// than assuming a fixed prefix.
func parseVersionToken(raw string) string {
	fields := strings.Fields(raw)
	if len(fields) == 0 {
		return ""
	}
	return fields[len(fields)-1]
}
