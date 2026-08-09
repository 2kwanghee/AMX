#!/bin/sh
# install-runner-guard.sh — force every `claude` invocation on this host through
# the AMX deliver-lock wrapper (deploy/amx-claude). B1 (러너 진입점 강제).
#
# Why a guard is needed
# ---------------------
# deploy/amx-claude closes the sub-second over-billing window described in
# docs/DEPLOYMENT-RUNNER.md, but ONLY for runners that are actually launched
# through it. A runner that calls the real `claude` binary directly bypasses the
# lock entirely. Code cannot prevent that; deployment must enforce the entrypoint.
#
# Mechanism chosen: PATH shadowing via a generated shim named `claude`
# -------------------------------------------------------------------
# We install an executable named `claude` into a bin directory that precedes the
# real claude on PATH (default /usr/local/bin). Alternatives were rejected:
#   * alias claude=amx-claude — shell aliases are expanded ONLY by interactive
#     shells. Non-interactive shells, `sh -c`, cron jobs, and systemd `ExecStart`
#     never see the alias, so the ClickEye webhook / batch runner path (the main
#     non-interactive entrypoint) would silently bypass the wrapper. Rejected.
#   * editing ~/.bashrc PATH — only sourced by login/interactive shells; same
#     cron/systemd blind spot as aliases. Rejected.
# A real file on PATH is resolved by execvp(3)/`command -v` regardless of shell
# interactivity, so cron and systemd units resolve it too, PROVIDED the guard dir
# is on their PATH. /usr/local/bin is on the systemd default PATH; for cron, add a
# `PATH=` line (see docs/DEPLOYMENT-RUNNER.md §7). We do not rename the real claude
# binary (fragile across npm/self-update); shadowing keeps upgrades of the real
# binary transparent.
#
# The shim exports AMX_CLAUDE_BIN=<real claude> and execs the wrapper, so the
# wrapper never recurses back into the shim even though both are named `claude`.
#
# Idempotent (safe to re-run; regenerates the shim) and fail-loud (any error
# aborts with non-zero and a diagnostic — never leaves a half-installed guard).
#
# Env overrides:
#   GUARD_BIN_DIR         where to install the shim (default /usr/local/bin)
#   GUARD_ALLOW_UNORDERED =1 to skip the "guard dir must precede real claude on
#                         PATH" safety check (enforcement would otherwise be a no-op)

set -eu

SENTINEL='AMX_RUNNER_GUARD_SHIM_V1'

fail() { printf 'install-runner-guard: ERROR: %s\n' "$1" >&2; exit 1; }

GUARD_BIN_DIR="${GUARD_BIN_DIR:-/usr/local/bin}"

# Locate the wrapper next to this script (absolute, so the shim can exec it from
# any cwd / any environment).
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd) || fail "cannot resolve script directory"
WRAPPER="$SCRIPT_DIR/amx-claude"
[ -f "$WRAPPER" ] || fail "wrapper not found: $WRAPPER"
[ -x "$WRAPPER" ] || fail "wrapper not executable: $WRAPPER"

SHIM="$GUARD_BIN_DIR/claude"

# Find the REAL claude: first `claude` on PATH that is neither the guard dir's
# shim nor our wrapper. Skipping GUARD_BIN_DIR makes re-runs resolve the real
# binary instead of a previously-installed shim (idempotency).
real_claude=''
saved_ifs=$IFS
IFS=:
for d in $PATH; do
	[ -n "$d" ] || d=.
	case "$d" in
	"$GUARD_BIN_DIR") continue ;;
	esac
	cand="$d/claude"
	if [ -x "$cand" ] && [ -f "$cand" ]; then
		# Skip anything that is itself a guard shim or the wrapper.
		if grep -q "$SENTINEL" "$cand" 2>/dev/null; then continue; fi
		if [ "$cand" = "$WRAPPER" ]; then continue; fi
		real_claude="$cand"
		break
	fi
done
IFS=$saved_ifs

[ -n "$real_claude" ] || fail "real 'claude' not found on PATH (excluding $GUARD_BIN_DIR); install Claude Code first"

# Enforcement is real only if the guard dir precedes the real claude's dir on the
# CURRENT PATH. If it does not, the shim would never be chosen — abort loudly.
real_dir=$(dirname "$real_claude")
if [ "${GUARD_ALLOW_UNORDERED:-0}" != "1" ]; then
	guard_pos=-1; real_pos=-1; i=0
	IFS=:
	for d in $PATH; do
		[ -n "$d" ] || d=.
		i=$((i + 1))
		if [ "$d" = "$GUARD_BIN_DIR" ] && [ "$guard_pos" -lt 0 ]; then guard_pos=$i; fi
		if [ "$d" = "$real_dir" ] && [ "$real_pos" -lt 0 ]; then real_pos=$i; fi
	done
	IFS=$saved_ifs
	if [ "$guard_pos" -lt 0 ]; then
		fail "guard dir '$GUARD_BIN_DIR' is not on PATH; the shim would never be resolved. Add it to PATH or set GUARD_ALLOW_UNORDERED=1 to override."
	fi
	if [ "$real_pos" -ge 0 ] && [ "$guard_pos" -gt "$real_pos" ]; then
		fail "guard dir '$GUARD_BIN_DIR' does not precede real claude dir '$real_dir' on PATH; enforcement would be a no-op. Fix PATH or set GUARD_ALLOW_UNORDERED=1 to override."
	fi
fi

# Create the guard dir if missing (fail-loud if we cannot).
mkdir -p "$GUARD_BIN_DIR" 2>/dev/null || fail "cannot create guard dir: $GUARD_BIN_DIR"
[ -w "$GUARD_BIN_DIR" ] || fail "guard dir not writable: $GUARD_BIN_DIR (run as an account that can write it)"

# Refuse to clobber a pre-existing NON-guard file at the shim path (e.g. the real
# claude living in the guard dir) — that would be data loss.
if [ -e "$SHIM" ] && ! grep -q "$SENTINEL" "$SHIM" 2>/dev/null; then
	fail "$SHIM exists and is not an AMX guard shim; refusing to overwrite"
fi

# Write the shim atomically (temp + rename) so a concurrent resolve never sees a
# half-written file.
tmp="$SHIM.amx-tmp.$$"
cat > "$tmp" <<EOF
#!/bin/sh
# $SENTINEL — generated by deploy/install-runner-guard.sh. Do not edit.
# Forces this 'claude' entrypoint through the AMX deliver-lock wrapper.
export AMX_CLAUDE_BIN="$real_claude"
exec "$WRAPPER" "\$@"
EOF
chmod 0755 "$tmp"
mv -f "$tmp" "$SHIM" || { rm -f "$tmp"; fail "cannot install shim at $SHIM"; }

printf 'install-runner-guard: OK\n'
printf '  shim installed : %s\n' "$SHIM"
printf '  real claude    : %s\n' "$real_claude"
printf '  wrapper        : %s\n' "$WRAPPER"
printf 'Run deploy/verify-runner-guard.sh to confirm enforcement.\n'
