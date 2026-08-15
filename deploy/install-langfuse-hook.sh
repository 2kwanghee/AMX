#!/bin/sh
# install-langfuse-hook.sh — wire the Langfuse tracing hook into a runner host
# (P3). Idempotent and fail-loud: re-running converges to the same state, any
# error aborts non-zero with a diagnostic.
#
# What it does
# ------------
#   1. Copies the vendored hook to  <CONFIG_DIR>/hooks/langfuse_hook.py
#   2. Merges a Stop hook entry into <CONFIG_DIR>/settings.json (other keys and
#      hooks preserved; re-running with the same command is a no-op).
#   3. Writes <CONFIG_DIR>/amx-langfuse.env (mode 0600) with the credentials and
#      TRACE_TO_LANGFUSE=true. The amx-claude wrapper sources this file and, only
#      then, exports the keys — so ONLY sessions launched through the wrapper are
#      traced; a host without this env file behaves exactly as before.
#
# Usage
# -----
#   LANGFUSE_BASE_URL=http://host:3100 \
#   LANGFUSE_PUBLIC_KEY=pk-... \
#   LANGFUSE_SECRET_KEY=sk-... \
#     sh deploy/install-langfuse-hook.sh
#
#   # or as flags:
#   sh deploy/install-langfuse-hook.sh \
#     --base-url http://host:3100 --public-key pk-... --secret-key sk-...
#
#   sh deploy/install-langfuse-hook.sh --uninstall   # remove env + Stop entry
#
# CONFIG_DIR defaults to $CLAUDE_CONFIG_DIR or ~/.claude (must match the wrapper
# and the AMA service account — see docs/DEPLOYMENT-RUNNER.md).

set -eu

die() { echo "install-langfuse-hook: $*" >&2; exit 1; }
info() { echo "install-langfuse-hook: $*"; }
# Escape a value for single-quoted shell: ' -> '\''
sq() { printf "%s" "$1" | sed "s/'/'\\\\''/g"; }

# Resolve the repo's deploy/langfuse dir relative to this script, so the install
# works from any CWD.
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd) || die "cannot resolve script dir"
SRC_HOOK="$SCRIPT_DIR/langfuse/langfuse_hook.py"

CONFIG_DIR=""   # resolved after arg parsing (see below)

UNINSTALL=0
BASE_URL="${LANGFUSE_BASE_URL:-}"
PUBLIC_KEY="${LANGFUSE_PUBLIC_KEY:-}"
SECRET_KEY="${LANGFUSE_SECRET_KEY:-}"

while [ $# -gt 0 ]; do
	case "$1" in
		--uninstall) UNINSTALL=1 ;;
		--base-url) shift; BASE_URL="${1:-}" ;;
		--public-key) shift; PUBLIC_KEY="${1:-}" ;;
		--secret-key) shift; SECRET_KEY="${1:-}" ;;
		--config-dir) shift; CONFIG_DIR="${1:-}" ;;
		-h|--help) sed -n '2,40p' "$0"; exit 0 ;;
		*) die "unknown argument: $1 (see --help)" ;;
	esac
	shift
done

# Config-home precedence, so the hook lands in the SAME home tsamx/amx-claude
# use on this host (see deploy/agent-setup.sh, default ~/.claude-amx):
#   --config-dir flag  >  $CLAUDE_CONFIG_DIR  >  ~/.claude-amx (if present)  >  ~/.claude
if [ -z "$CONFIG_DIR" ]; then
	if [ -n "${CLAUDE_CONFIG_DIR:-}" ]; then
		CONFIG_DIR="$CLAUDE_CONFIG_DIR"
	elif [ -d "$HOME/.claude-amx" ]; then
		CONFIG_DIR="$HOME/.claude-amx"
	else
		CONFIG_DIR="$HOME/.claude"
	fi
fi
info "config home: $CONFIG_DIR"

command -v python3 >/dev/null 2>&1 || die "python3 is required (for settings.json merge)"

HOOKS_DIR="$CONFIG_DIR/hooks"
DEST_HOOK="$HOOKS_DIR/langfuse_hook.py"
SETTINGS="$CONFIG_DIR/settings.json"
ENV_FILE="$CONFIG_DIR/amx-langfuse.env"
# The command claude runs at Stop. `uv run --script` reads the inline
# dependency metadata in the hook and installs langfuse into an ephemeral env.
HOOK_CMD="uv run --script $DEST_HOOK"

# ---- settings.json merge/removal (idempotent, preserves other keys) ---------
# python reads the settings file BY PATH (never via stdin — stdin carries the
# script here) so all sibling keys/hooks are preserved. Adds the Stop entry only
# when absent; --uninstall removes matching entries. Writes atomically and
# prints CHANGED / UNCHANGED so the shell reports without a second write.
apply_settings() {
	_mode="$1"
	_res=$(python3 - "$_mode" "$HOOK_CMD" "$SETTINGS" <<'PY'
import copy, json, os, sys

mode, cmd, path = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path) as f:
        raw = f.read().strip()
except FileNotFoundError:
    raw = ""
try:
    data = json.loads(raw) if raw else {}
    if not isinstance(data, dict):
        raise ValueError
except Exception:
    sys.stderr.write("settings.json is not valid JSON; refusing to touch it\n")
    sys.exit(3)

before = copy.deepcopy(data)

hooks = data.get("hooks")
if not isinstance(hooks, dict):
    hooks = {}
stop = hooks.get("Stop")
if not isinstance(stop, list):
    stop = []

def has_cmd(block):
    return any(isinstance(h, dict) and h.get("type") == "command" and h.get("command") == cmd
              for h in block.get("hooks", []))

if mode == "install":
    if not any(isinstance(b, dict) and has_cmd(b) for b in stop):
        stop.append({"hooks": [{"type": "command", "command": cmd}]})
    hooks["Stop"] = stop
    data["hooks"] = hooks
else:  # uninstall
    new_stop = []
    for b in stop:
        if not isinstance(b, dict):
            new_stop.append(b); continue
        kept = [h for h in b.get("hooks", [])
                if not (isinstance(h, dict) and h.get("type") == "command" and h.get("command") == cmd)]
        if kept:
            nb = dict(b); nb["hooks"] = kept; new_stop.append(nb)
        # a block whose only hook was ours is dropped entirely
    if new_stop:
        hooks["Stop"] = new_stop
    else:
        hooks.pop("Stop", None)
    data["hooks"] = hooks
    if not hooks:
        data.pop("hooks", None)

if data == before:
    print("UNCHANGED")
    sys.exit(0)

tmp = path + ".tmp.%d" % os.getpid()
with open(tmp, "w") as f:
    f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
os.replace(tmp, path)
print("CHANGED")
PY
	) || die "failed to merge settings.json"
	case "$_res" in
		UNCHANGED) info "settings.json already up to date (no change)" ;;
		CHANGED)   info "settings.json updated ($_mode Stop hook)" ;;
		*)         die "unexpected settings merge result: $_res" ;;
	esac
}

if [ "$UNINSTALL" = 1 ]; then
	[ -d "$CONFIG_DIR" ] || die "config dir not found: $CONFIG_DIR"
	apply_settings uninstall
	if [ -f "$ENV_FILE" ]; then
		rm -f "$ENV_FILE" && info "removed $ENV_FILE"
	else
		info "no env file to remove ($ENV_FILE)"
	fi
	info "left hook script in place: $DEST_HOOK (safe; inert without env file)"
	info "uninstall complete"
	exit 0
fi

# ---- install ----------------------------------------------------------------
[ -f "$SRC_HOOK" ] || die "vendored hook not found: $SRC_HOOK"
[ -n "$BASE_URL" ] || die "LANGFUSE_BASE_URL is required (env or --base-url)"
[ -n "$PUBLIC_KEY" ] || die "LANGFUSE_PUBLIC_KEY is required (env or --public-key)"
[ -n "$SECRET_KEY" ] || die "LANGFUSE_SECRET_KEY is required (env or --secret-key)"

# Integrity: verify the vendored hook against the recorded checksum BEFORE
# copying it into the runner's config home. python3 (already required) does the
# hashing, so we do not depend on sha256sum(1) being present.
SUMS_FILE="$SCRIPT_DIR/langfuse/SHA256SUMS"
[ -f "$SUMS_FILE" ] || die "checksum file not found: $SUMS_FILE"
python3 - "$SRC_HOOK" "$SUMS_FILE" <<'PY' || die "hook checksum verification failed — refusing to install"
import hashlib, sys
src, sums = sys.argv[1], sys.argv[2]
want = ""
for line in open(sums):
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    h, _, name = line.partition("  ")
    if name.strip() == "langfuse_hook.py":
        want = h.strip().lower()
        break
if not want:
    sys.stderr.write("no langfuse_hook.py entry in SHA256SUMS\n"); sys.exit(1)
got = hashlib.sha256(open(src, "rb").read()).hexdigest()
if got != want:
    sys.stderr.write("checksum mismatch: expected %s got %s\n" % (want, got)); sys.exit(1)
PY
info "verified hook checksum"

# 'uv' is mandatory: the Stop hook runs via `uv run --script`, which reads the
# hook's inline dependency metadata and installs langfuse into an ephemeral env.
command -v uv >/dev/null 2>&1 || die "'uv' is required on PATH (Stop hook runs 'uv run --script'); install from https://docs.astral.sh/uv/"

mkdir -p "$HOOKS_DIR" || die "cannot create $HOOKS_DIR"
cp "$SRC_HOOK" "$DEST_HOOK" || die "cannot copy hook to $DEST_HOOK"
info "installed hook: $DEST_HOOK"

apply_settings install

# ---- env file (0600, backup existing) ---------------------------------------
if [ -f "$ENV_FILE" ]; then
	cp "$ENV_FILE" "$ENV_FILE.bak" || die "cannot back up $ENV_FILE"
	info "backed up existing env file to $ENV_FILE.bak"
fi
_tmp="$ENV_FILE.tmp.$$"
umask 077
cat > "$_tmp" <<EOF
# amx-langfuse.env — sourced by deploy/amx-claude. Only sessions launched
# through that wrapper are traced; delete this file to turn tracing off.
# Managed by deploy/install-langfuse-hook.sh (re-run to regenerate).
TRACE_TO_LANGFUSE=true
LANGFUSE_BASE_URL='$(sq "$BASE_URL")'
LANGFUSE_PUBLIC_KEY='$(sq "$PUBLIC_KEY")'
LANGFUSE_SECRET_KEY='$(sq "$SECRET_KEY")'
# LANGFUSE_USER_ID and LANGFUSE_TRACING_ENVIRONMENT are auto-derived by the
# wrapper (active tsamx account email / hostname) when left unset. Pin them here
# to override.
# LANGFUSE_USER_ID=
# LANGFUSE_TRACING_ENVIRONMENT=
# Truncate large prompt/response payloads (chars). Default 20000.
# CC_LANGFUSE_MAX_CHARS=20000
# CC_LANGFUSE_DEBUG=true
EOF
chmod 600 "$_tmp" || die "cannot chmod env file"
mv "$_tmp" "$ENV_FILE" || die "cannot write $ENV_FILE"
info "wrote $ENV_FILE (mode 0600)"

# Warm the uv dependency cache now (while this install presumably has network),
# so the FIRST real Stop hook on an offline/air-gapped host does not have to
# resolve langfuse from PyPI. Run with TRACE_TO_LANGFUSE unset and no keys, so
# the hook takes its inactive early-exit path — it only forces uv to fetch and
# cache the pinned dependencies. Best-effort: a failure is a warning, not fatal.
info "warming uv dependency cache (first-run offline safety)..."
if env -u TRACE_TO_LANGFUSE -u LANGFUSE_PUBLIC_KEY -u LANGFUSE_SECRET_KEY \
	uv run --script "$DEST_HOOK" </dev/null >/dev/null 2>&1; then
	info "uv cache warmed"
else
	info "warning: could not warm uv cache now; the first Stop hook will resolve"
	info "         dependencies then (needs network at that point)."
fi

info "done. Sessions launched via amx-claude on this host will trace to Langfuse."
