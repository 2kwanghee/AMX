#!/bin/sh
# verify-runner-guard.sh — judge whether the B1 runner-entrypoint guard is in
# effect on this host. Use once right after install-runner-guard.sh, and again as
# a periodic health check (cron/systemd timer). Exit 0 = enforced, non-zero = not.
#
# Two independent judgements (both must hold for exit 0):
#   (a) `claude` resolves to the AMX wrapper — i.e. the name a runner would exec
#       lands on the guard shim / deploy/amx-claude, not the bare real binary.
#   (b) the runner account and the AMA service account see the SAME ~/.claude
#       (CLAUDE_CONFIG_DIR). If they diverge (different HOME, container mount, or
#       a stray CLAUDE_CONFIG_DIR), AMA's deliver lock and the runner's lock live
#       in different files and the flock coordination is void.
#
# (b) needs to know the AMA account's config dir. Provide ONE of:
#   AMA_CLAUDE_CONFIG_DIR  explicit path AMA uses (e.g. /home/ama/.claude)
#   AMA_USER               AMA's unix account; its ~/.claude is derived via passwd
# The runner-side config dir is CLAUDE_CONFIG_DIR (default ~/.claude), matching
# how deploy/amx-claude resolves it.

set -eu

SENTINEL='AMX_RUNNER_GUARD_SHIM_V1'
rc=0
pass() { printf '[ OK ] %s\n' "$1"; }
warn() { printf '[WARN] %s\n' "$1"; }
bad()  { printf '[FAIL] %s\n' "$1"; rc=1; }

# realpath helper (portable): prefer realpath, then readlink -f, else best-effort.
canon() {
	if command -v realpath >/dev/null 2>&1; then realpath -m "$1" 2>/dev/null && return 0; fi
	if command -v readlink >/dev/null 2>&1; then readlink -f "$1" 2>/dev/null && return 0; fi
	printf '%s\n' "$1"
}

printf '== AMX runner-guard verification ==\n'

# ---- (a) does `claude` resolve to the wrapper? ------------------------------
resolved=$(command -v claude 2>/dev/null || true)
if [ -z "$resolved" ]; then
	bad "(a) 'claude' is not on PATH — nothing to enforce, runner cannot start"
else
	# Follow one symlink level to reach the actual file if needed.
	target="$resolved"
	if [ -L "$resolved" ]; then target=$(canon "$resolved"); fi
	if grep -q "$SENTINEL" "$target" 2>/dev/null; then
		pass "(a) 'claude' -> guard shim: $resolved"
	elif [ "$(basename "$target")" = "amx-claude" ]; then
		pass "(a) 'claude' -> wrapper amx-claude directly: $resolved"
	else
		bad "(a) 'claude' resolves to '$resolved', which is NOT the AMX wrapper — direct/unguarded runner. Run deploy/install-runner-guard.sh."
	fi
fi

# ---- (b) do runner and AMA see the same ~/.claude? --------------------------
runner_cfg="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
runner_canon=$(canon "$runner_cfg")

ama_cfg="${AMA_CLAUDE_CONFIG_DIR:-}"
if [ -z "$ama_cfg" ] && [ -n "${AMA_USER:-}" ]; then
	ama_home=$(getent passwd "$AMA_USER" 2>/dev/null | cut -d: -f6 || true)
	[ -n "$ama_home" ] || ama_home=$(eval printf '%s' "~$AMA_USER" 2>/dev/null || true)
	if [ -n "$ama_home" ] && [ "$ama_home" != "~$AMA_USER" ]; then
		ama_cfg="$ama_home/.claude"
	fi
fi

if [ -z "$ama_cfg" ]; then
	bad "(b) AMA config dir unknown — set AMA_CLAUDE_CONFIG_DIR or AMA_USER so runner/AMA ~/.claude equality can be judged"
else
	ama_canon=$(canon "$ama_cfg")
	if [ "$runner_canon" = "$ama_canon" ]; then
		pass "(b) runner and AMA share ~/.claude: $runner_canon"
	else
		bad "(b) config dir MISMATCH — runner sees '$runner_canon' but AMA sees '$ama_canon'; flock coordination is void (different HOME/mount/CLAUDE_CONFIG_DIR)"
	fi
fi

printf '== result: '
if [ "$rc" -eq 0 ]; then printf 'ENFORCED (pass) ==\n'; else printf 'NOT ENFORCED (fail) ==\n'; fi
exit "$rc"
