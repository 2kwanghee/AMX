#!/bin/sh
# verify-runner-guard.sh — judge whether the B1 runner-entrypoint guard is in
# effect on this host. Use once right after install-runner-guard.sh, and again as
# a periodic health check (cron/systemd timer). Exit 0 = enforced, non-zero = not.
#
# Two independent judgements per runner (both must hold for exit 0):
#   (a) the runner name resolves to the AMX wrapper — i.e. the name a runner
#       would exec lands on the guard shim / deploy/amx-<name>, not the bare
#       real binary.
#   (b) the runner account and the AMA service account see the SAME config dir.
#       If they diverge (different HOME, container mount, or a stray
#       CLAUDE_CONFIG_DIR / CODEX_HOME), AMA's deliver lock and the runner's lock
#       live in different files and the flock coordination is void.
#
# claude is mandatory: a host that cannot resolve it fails. codex is optional, so
# that this stays usable on claude-only deployments: no codex on PATH is a SKIP,
# and a codex whose AMA-side config dir was never declared is a WARN. What is
# never tolerated for either runner is a name that resolves PAST the wrapper, or
# two config dirs that demonstrably differ — both mean the lock is void.
#
# (b) needs to know the AMA account's config dir:
#   claude — AMA_CLAUDE_CONFIG_DIR, or AMA_USER (its ~/.claude is derived via
#            passwd, which is safe because ~/.claude IS the documented default
#            on both sides)
#   codex  — AMA_CODEX_CONFIG_DIR / AMA_CODEX_HOME only. NOT derived from
#            AMA_USER: the agent stages Codex credentials solely into
#            AMX_CODEX_HOME and has no ~/.codex fallback, so a derived guess
#            would compare a directory neither side uses and pass.
# Runner-side dirs are CLAUDE_CONFIG_DIR (default ~/.claude) and CODEX_HOME /
# AMX_CODEX_HOME, matching deploy/amx-claude and deploy/amx-codex.

set -eu

SENTINEL='AMX_RUNNER_GUARD_SHIM_V1'
rc=0
pass() { printf '[ OK ] %s\n' "$1"; }
skip() { printf '[SKIP] %s\n' "$1"; }
warn() { printf '[WARN] %s\n' "$1"; }
bad()  { printf '[FAIL] %s\n' "$1"; rc=1; }

# realpath helper (portable): prefer realpath, then readlink -f, else best-effort.
canon() {
	if command -v realpath >/dev/null 2>&1; then realpath -m "$1" 2>/dev/null && return 0; fi
	if command -v readlink >/dev/null 2>&1; then readlink -f "$1" 2>/dev/null && return 0; fi
	printf '%s\n' "$1"
}

ama_home_dir() {
	[ -n "${AMA_USER:-}" ] || return 0
	_home=$(getent passwd "$AMA_USER" 2>/dev/null | cut -d: -f6 || true)
	[ -n "$_home" ] || _home=$(eval printf '%s' "~$AMA_USER" 2>/dev/null || true)
	[ "$_home" != "~$AMA_USER" ] || _home=''
	printf '%s' "$_home"
}

# check_resolution <name> — judgement (a), as a verdict string on stdout:
# `unresolved`, `shim:<path>`, `wrapper:<path>` or `unguarded:<path>`. It runs
# inside a command substitution, so it prints no verdict itself and touches no
# rc — a subshell's assignment to rc would be discarded.
check_resolution() {
	_name=$1
	_resolved=$(command -v "$_name" 2>/dev/null || true)
	if [ -z "$_resolved" ]; then
		printf 'unresolved'
		return 0
	fi
	# Follow one symlink level to reach the actual file if needed.
	_target="$_resolved"
	if [ -L "$_resolved" ]; then _target=$(canon "$_resolved"); fi
	if grep -q "$SENTINEL" "$_target" 2>/dev/null; then
		printf 'shim:%s' "$_resolved"
	elif [ "$(basename "$_target")" = "amx-$_name" ]; then
		printf 'wrapper:%s' "$_resolved"
	else
		printf 'unguarded:%s' "$_resolved"
	fi
}

# report_resolution <name> <verdict> — turn a verdict into a judgement + rc.
# `unresolved` is left to the caller: mandatory for claude, a skip for codex.
report_resolution() {
	case "$2" in
	shim:*)      pass "(a) '$1' -> guard shim: ${2#shim:}" ;;
	wrapper:*)   pass "(a) '$1' -> wrapper amx-$1 directly: ${2#wrapper:}" ;;
	unguarded:*) bad "(a) '$1' resolves to '${2#unguarded:}', which is NOT the AMX wrapper — direct/unguarded runner. Run deploy/install-runner-guard.sh." ;;
	esac
}

# check_config_dir <label> <runner_dir> <ama_dir> <unknown_is_fatal:0|1> —
# judgement (b). A real MISMATCH always fails. Not knowing AMA's dir is fatal
# for claude (every AMX host runs a Claude runner, so the check must be
# conclusive) but only a warning for codex: the mere presence of a codex binary
# does not mean this host was ever given a Codex account, and a claude-only
# deployment must not go red because of one.
check_config_dir() {
	_label=$1
	_runner_canon=$(canon "$2")
	_ama=$3
	if [ -z "$_ama" ]; then
		_msg="(b) $_label: AMA config dir unknown — set AMA_${_label}_CONFIG_DIR or AMA_USER so runner/AMA equality can be judged"
		if [ "$4" = "1" ]; then bad "$_msg"; else warn "$_msg"; fi
		return 0
	fi
	_ama_canon=$(canon "$_ama")
	if [ "$_runner_canon" = "$_ama_canon" ]; then
		pass "(b) $_label: runner and AMA share $_runner_canon"
	else
		bad "(b) $_label: config dir MISMATCH — runner sees '$_runner_canon' but AMA sees '$_ama_canon'; flock coordination is void (different HOME/mount/env)"
	fi
}

printf '== AMX runner-guard verification ==\n'
ama_home=$(ama_home_dir)

# ---- claude (mandatory) ------------------------------------------------------
claude_state=$(check_resolution claude)
if [ "$claude_state" = "unresolved" ]; then
	bad "(a) 'claude' is not on PATH — nothing to enforce, runner cannot start"
else
	report_resolution claude "$claude_state"
fi

ama_claude="${AMA_CLAUDE_CONFIG_DIR:-}"
if [ -z "$ama_claude" ] && [ -n "$ama_home" ]; then ama_claude="$ama_home/.claude"; fi
check_config_dir CLAUDE "${CLAUDE_CONFIG_DIR:-$HOME/.claude}" "$ama_claude" 1

# ---- codex (optional until installed) ---------------------------------------
codex_state=$(check_resolution codex)
if [ "$codex_state" = "unresolved" ]; then
	# No codex on this host: nothing to guard, and nothing to fail. A codex
	# account cannot be delivered here either — the agent needs the binary.
	skip "(a) 'codex' is not on PATH — host runs no Codex runner"
else
	report_resolution codex "$codex_state"
	# Deliberately NOT derived from AMA_USER's ~/.codex the way claude's is. The
	# agent stages Codex credentials only into AMX_CODEX_HOME and has no ~/.codex
	# fallback, so guessing ~/.codex on both sides makes the two agree on a
	# directory neither side actually uses — this check would print [ OK ] for
	# precisely the misconfiguration it exists to catch. An undeclared AMA dir is
	# reported as unjudged instead.
	runner_codex="${CODEX_HOME:-${AMX_CODEX_HOME:-}}"
	if [ -z "$runner_codex" ]; then
		warn "(b) CODEX: neither CODEX_HOME nor AMX_CODEX_HOME is set — amx-codex would fall back to ~/.codex while AMA stages only into AMX_CODEX_HOME; set both to the same directory"
	else
		check_config_dir CODEX "$runner_codex" "${AMA_CODEX_CONFIG_DIR:-${AMA_CODEX_HOME:-}}" 0
	fi
fi

printf '== result: '
if [ "$rc" -eq 0 ]; then printf 'ENFORCED (pass) ==\n'; else printf 'NOT ENFORCED (fail) ==\n'; fi
exit "$rc"
