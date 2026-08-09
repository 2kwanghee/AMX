#!/usr/bin/env bash
# test-runner-guard.sh — self-test for install-runner-guard.sh and
# verify-runner-guard.sh. Uses a throwaway temp dir with a fake `claude` binary
# and a fake HOME. No docker, no root. Run: bash deploy/test-runner-guard.sh
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
INSTALL="$here/install-runner-guard.sh"
VERIFY="$here/verify-runner-guard.sh"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

fails=0
ok()   { printf '  PASS: %s\n' "$1"; }
nok()  { printf '  FAIL: %s\n' "$1"; fails=$((fails + 1)); }

# --- fixtures --------------------------------------------------------------
realbin="$tmp/realbin"; mkdir -p "$realbin"
cat > "$realbin/claude" <<'EOF'
#!/bin/sh
echo "REAL-CLAUDE-RAN args=$*"
EOF
chmod +x "$realbin/claude"

guarddir="$tmp/guard"; mkdir -p "$guarddir"
home="$tmp/home"; mkdir -p "$home/.claude"
otherhome="$tmp/other"; mkdir -p "$otherhome/.claude"

# guard dir must precede the real claude dir on PATH
base_path="$guarddir:$realbin:/usr/bin:/bin"

export GUARD_BIN_DIR="$guarddir"
export AMX_DELIVER_WAIT=1   # keep the wrapper's flock wait short in tests

# ==========================================================================
echo "[1] pre-install: bypass state must be detected as NOT ENFORCED"
if PATH="$base_path" HOME="$home" \
   CLAUDE_CONFIG_DIR="$home/.claude" AMA_CLAUDE_CONFIG_DIR="$home/.claude" \
   bash "$VERIFY" >/dev/null 2>&1; then
	nok "verify passed while unguarded (should have failed)"
else
	ok "verify correctly reports unguarded state"
fi

# ==========================================================================
echo "[2] install is fail-loud when guard dir does not precede real claude"
if PATH="$realbin:$guarddir:/usr/bin:/bin" HOME="$home" \
   bash "$INSTALL" >/dev/null 2>&1; then
	nok "install succeeded despite wrong PATH ordering (should fail-loud)"
else
	ok "install fails loudly on non-enforcing PATH ordering"
fi

# ==========================================================================
echo "[3] install (and idempotent re-install) succeed"
if PATH="$base_path" HOME="$home" bash "$INSTALL" >/dev/null 2>&1; then
	ok "install succeeded"
else
	nok "install failed"
fi
if PATH="$base_path" HOME="$home" bash "$INSTALL" >/dev/null 2>&1; then
	ok "re-install (idempotent) succeeded"
else
	nok "re-install failed"
fi
[ -x "$guarddir/claude" ] && ok "shim present at $guarddir/claude" || nok "shim missing"

# ==========================================================================
echo "[4] enforced state: verify passes and claude resolves to the shim"
if PATH="$base_path" HOME="$home" \
   CLAUDE_CONFIG_DIR="$home/.claude" AMA_CLAUDE_CONFIG_DIR="$home/.claude" \
   bash "$VERIFY" >/dev/null 2>&1; then
	ok "verify reports ENFORCED after install"
else
	nok "verify failed after install"
fi
resolved=$(PATH="$base_path" command -v claude)
[ "$resolved" = "$guarddir/claude" ] && ok "claude resolves to shim first" \
	|| nok "claude resolved to '$resolved' (expected shim)"

# ==========================================================================
echo "[5] shim passes through to the real claude via the wrapper (no recursion)"
out=$(PATH="$base_path" HOME="$home" claude -p hello 2>/dev/null || true)
if printf '%s' "$out" | grep -q 'REAL-CLAUDE-RAN args=-p hello'; then
	ok "shim -> wrapper -> real claude, args passed through"
else
	nok "passthrough failed; got: $out"
fi

# ==========================================================================
echo "[6] (b) divergent ~/.claude between runner and AMA is detected"
if PATH="$base_path" HOME="$home" \
   CLAUDE_CONFIG_DIR="$home/.claude" AMA_CLAUDE_CONFIG_DIR="$otherhome/.claude" \
   bash "$VERIFY" >/dev/null 2>&1; then
	nok "verify passed with mismatched config dirs (should fail)"
else
	ok "verify detects mismatched runner/AMA config dirs"
fi

# ==========================================================================
echo "[7] (b) missing AMA config info is treated as NOT judged (fail)"
if PATH="$base_path" HOME="$home" CLAUDE_CONFIG_DIR="$home/.claude" \
   bash "$VERIFY" >/dev/null 2>&1; then
	nok "verify passed without AMA config info (should fail)"
else
	ok "verify fails when AMA config dir is unknown"
fi

# --------------------------------------------------------------------------
echo
if [ "$fails" -eq 0 ]; then
	echo "ALL TESTS PASSED"
	exit 0
else
	echo "$fails TEST(S) FAILED"
	exit 1
fi
