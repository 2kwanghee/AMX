#!/bin/sh
# install-runner-guard.sh — force every runner invocation on this host through
# the matching AMX deliver-lock wrapper. B1 (러너 진입점 강제).
#
# Covers both runners AMX manages:
#   claude -> deploy/amx-claude   (mandatory; absence is an error)
#   codex  -> deploy/amx-codex    (optional; skipped when codex is not installed)
#
# Why a guard is needed
# ---------------------
# The wrappers close the sub-second over-billing window described in
# docs/DEPLOYMENT-RUNNER.md, but ONLY for runners that are actually launched
# through them. A runner that calls the real binary directly bypasses the lock
# entirely. Code cannot prevent that; deployment must enforce the entrypoint.
#
# Mechanism chosen: PATH shadowing via a generated shim
# -----------------------------------------------------
# We install an executable named `claude` (resp. `codex`) into a bin directory
# that precedes the real binary on PATH (default /usr/local/bin). Alternatives
# were rejected:
#   * alias claude=amx-claude — shell aliases are expanded ONLY by interactive
#     shells. Non-interactive shells, `sh -c`, cron jobs, and systemd `ExecStart`
#     never see the alias, so the ClickEye webhook / batch runner path (the main
#     non-interactive entrypoint) would silently bypass the wrapper. Rejected.
#   * editing ~/.bashrc PATH — only sourced by login/interactive shells; same
#     cron/systemd blind spot as aliases. Rejected.
# A real file on PATH is resolved by execvp(3)/`command -v` regardless of shell
# interactivity, so cron and systemd units resolve it too, PROVIDED the guard dir
# is on their PATH. /usr/local/bin is on the systemd default PATH; for cron, add a
# `PATH=` line (see docs/DEPLOYMENT-RUNNER.md §7). We do not rename the real
# binary (fragile across npm/self-update); shadowing keeps upgrades of the real
# binary transparent.
#
# The shim exports AMX_CLAUDE_BIN / AMX_CODEX_BIN =<real binary> and execs the
# wrapper, so the wrapper never recurses back into the shim even though both
# carry the same name.
#
# Idempotent (safe to re-run; regenerates the shims) and fail-loud (any error
# aborts with non-zero and a diagnostic — never leaves a half-installed guard).
#
# Env overrides:
#   GUARD_BIN_DIR         where to install the shims (default /usr/local/bin)
#   GUARD_ALLOW_UNORDERED =1 to skip the "guard dir must precede the real binary
#                         on PATH" safety check (enforcement would otherwise be a
#                         no-op)
#   GUARD_SKIP_CODEX      =1 to leave codex alone even when it is installed

set -eu

SENTINEL='AMX_RUNNER_GUARD_SHIM_V1'

fail() { printf 'install-runner-guard: ERROR: %s\n' "$1" >&2; exit 1; }
note() { printf 'install-runner-guard: %s\n' "$1"; }

GUARD_BIN_DIR="${GUARD_BIN_DIR:-/usr/local/bin}"

# Locate the wrappers next to this script (absolute, so a shim can exec one from
# any cwd / any environment).
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd) || fail "cannot resolve script directory"

# Create the guard dir if missing (fail-loud if we cannot).
mkdir -p "$GUARD_BIN_DIR" 2>/dev/null || fail "cannot create guard dir: $GUARD_BIN_DIR"
[ -w "$GUARD_BIN_DIR" ] || fail "guard dir not writable: $GUARD_BIN_DIR (run as an account that can write it)"

# find_real_binary <name> — first `<name>` on PATH that is neither the guard
# dir's shim nor one of our wrappers, echoed on stdout (empty if none).
# Skipping GUARD_BIN_DIR makes re-runs resolve the real binary instead of a
# previously-installed shim (idempotency).
find_real_binary() {
	_name=$1
	_found=''
	_saved_ifs=$IFS
	IFS=:
	for d in $PATH; do
		[ -n "$d" ] || d=.
		case "$d" in
		"$GUARD_BIN_DIR") continue ;;
		esac
		cand="$d/$_name"
		if [ -x "$cand" ] && [ -f "$cand" ]; then
			# Skip anything that is itself a guard shim or an AMX wrapper.
			if grep -q "$SENTINEL" "$cand" 2>/dev/null; then continue; fi
			case "$cand" in
			"$SCRIPT_DIR/amx-$_name") continue ;;
			esac
			_found=$cand
			break
		fi
	done
	IFS=$_saved_ifs
	printf '%s' "$_found"
}

# check_path_order <real_binary> — abort unless the guard dir precedes the real
# binary's dir on the CURRENT PATH. If it does not, the shim would never be
# chosen and installing it would give a false sense of enforcement.
check_path_order() {
	[ "${GUARD_ALLOW_UNORDERED:-0}" != "1" ] || return 0
	_real_dir=$(dirname "$1")
	_guard_pos=-1; _real_pos=-1; _i=0
	_saved_ifs=$IFS
	IFS=:
	for d in $PATH; do
		[ -n "$d" ] || d=.
		_i=$((_i + 1))
		if [ "$d" = "$GUARD_BIN_DIR" ] && [ "$_guard_pos" -lt 0 ]; then _guard_pos=$_i; fi
		if [ "$d" = "$_real_dir" ] && [ "$_real_pos" -lt 0 ]; then _real_pos=$_i; fi
	done
	IFS=$_saved_ifs
	if [ "$_guard_pos" -lt 0 ]; then
		fail "guard dir '$GUARD_BIN_DIR' is not on PATH; the shim would never be resolved. Add it to PATH or set GUARD_ALLOW_UNORDERED=1 to override."
	fi
	if [ "$_real_pos" -ge 0 ] && [ "$_guard_pos" -gt "$_real_pos" ]; then
		fail "guard dir '$GUARD_BIN_DIR' does not precede real $_real_dir on PATH; enforcement would be a no-op. Fix PATH or set GUARD_ALLOW_UNORDERED=1 to override."
	fi
}

# install_shim <name> <BIN_ENV_VAR> <required:0|1>
install_shim() {
	_name=$1
	_env=$2
	_required=$3
	_wrapper="$SCRIPT_DIR/amx-$_name"

	if [ ! -f "$_wrapper" ]; then
		# `if`, not `[ … ] && fail`: under `set -e` a false test as the last
		# command of an AND-OR list is itself a non-zero status and would abort
		# the run silently instead of skipping.
		if [ "$_required" = "1" ]; then fail "wrapper not found: $_wrapper"; fi
		note "skip $_name: wrapper $_wrapper not present"
		return 0
	fi
	[ -x "$_wrapper" ] || fail "wrapper not executable: $_wrapper"

	_real=$(find_real_binary "$_name")
	if [ -z "$_real" ]; then
		# Before concluding "not installed", check whether the only copy is the
		# one INSIDE the guard dir — npm's default prefix is /usr/local/bin, so
		# the real binary frequently lands exactly where the shim wants to go.
		# find_real_binary skips that dir for idempotency, which would otherwise
		# make an unguardable host look like a host with nothing to guard.
		_occupant="$GUARD_BIN_DIR/$_name"
		if [ -e "$_occupant" ] && ! grep -q "$SENTINEL" "$_occupant" 2>/dev/null; then
			fail "the only '$_name' on PATH is $_occupant, inside the guard dir, so PATH shadowing cannot cover it. Move the real binary out of $GUARD_BIN_DIR (or point GUARD_BIN_DIR at a directory earlier on PATH) and re-run."
		fi
		# An optional runner that is not installed needs no guard: shimming it
		# would only produce a `codex` on PATH that cannot exec anything.
		if [ "$_required" = "1" ]; then
			fail "real '$_name' not found on PATH (excluding $GUARD_BIN_DIR); install Claude Code first"
		fi
		note "skip $_name: not installed on this host"
		return 0
	fi

	check_path_order "$_real"

	_shim="$GUARD_BIN_DIR/$_name"
	# Refuse to clobber a pre-existing NON-guard file at the shim path (e.g. the
	# real binary living in the guard dir) — that would be data loss.
	if [ -e "$_shim" ] && ! grep -q "$SENTINEL" "$_shim" 2>/dev/null; then
		fail "$_shim exists and is not an AMX guard shim; refusing to overwrite"
	fi

	# Write the shim atomically (temp + rename) so a concurrent resolve never
	# sees a half-written file.
	_tmp="$_shim.amx-tmp.$$"
	cat > "$_tmp" <<EOF
#!/bin/sh
# $SENTINEL — generated by deploy/install-runner-guard.sh. Do not edit.
# Forces this '$_name' entrypoint through the AMX deliver-lock wrapper.
export $_env="$_real"
exec "$_wrapper" "\$@"
EOF
	chmod 0755 "$_tmp"
	mv -f "$_tmp" "$_shim" || { rm -f "$_tmp"; fail "cannot install shim at $_shim"; }

	printf 'install-runner-guard: OK (%s)\n' "$_name"
	printf '  shim installed : %s\n' "$_shim"
	printf '  real %-10s: %s\n' "$_name" "$_real"
	printf '  wrapper        : %s\n' "$_wrapper"
}

install_shim claude AMX_CLAUDE_BIN 1
if [ "${GUARD_SKIP_CODEX:-0}" = "1" ]; then
	note "skip codex: GUARD_SKIP_CODEX=1"
else
	install_shim codex AMX_CODEX_BIN 0
fi

printf 'Run deploy/verify-runner-guard.sh to confirm enforcement.\n'
