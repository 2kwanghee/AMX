#!/bin/sh
# install-langfuse-hook.sh — wire the Langfuse tracing hook into a runner host
# (P3). Idempotent and fail-loud: re-running converges to the same state, any
# error aborts non-zero with a diagnostic.
#
# What it does
# ------------
#   1. Copies the vendored hook to  <CONFIG_DIR>/hooks/langfuse_hook.py and the
#      self-authored session cost-structure hook to session_usage_hook.py
#   2. Merges TWO Stop hook entries into <CONFIG_DIR>/settings.json — the langfuse
#      tracer and the session-usage reporter coexist on the same event (other keys
#      and hooks preserved; re-running with the same commands is a no-op).
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
#   # also wire the PreToolUse danger-command detection hook (opt-in, off by default):
#   sh deploy/install-langfuse-hook.sh --with-danger-hook \
#     --base-url http://host:3100 --public-key pk-... --secret-key sk-...
#
#   sh deploy/install-langfuse-hook.sh --uninstall   # remove env + both Stop + danger entries
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
SRC_DANGER_HOOK="$SCRIPT_DIR/langfuse/danger_hook.py"
SRC_SESSION_HOOK="$SCRIPT_DIR/langfuse/session_usage_hook.py"

CONFIG_DIR=""   # resolved after arg parsing (see below)

UNINSTALL=0
WITH_DANGER=0
BASE_URL="${LANGFUSE_BASE_URL:-}"
PUBLIC_KEY="${LANGFUSE_PUBLIC_KEY:-}"
SECRET_KEY="${LANGFUSE_SECRET_KEY:-}"

while [ $# -gt 0 ]; do
	case "$1" in
		--uninstall) UNINSTALL=1 ;;
		--with-danger-hook) WITH_DANGER=1 ;;
		--base-url) shift; BASE_URL="${1:-}" ;;
		--public-key) shift; PUBLIC_KEY="${1:-}" ;;
		--secret-key) shift; SECRET_KEY="${1:-}" ;;
		--config-dir) shift; CONFIG_DIR="${1:-}" ;;
		# 헤더 주석 블록만 출력한다(범위는 위 헤더 길이에 맞춘다).
		-h|--help) sed -n '2,36p' "$0"; exit 0 ;;
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
DEST_DANGER_HOOK="$HOOKS_DIR/danger_hook.py"
DEST_SESSION_HOOK="$HOOKS_DIR/session_usage_hook.py"
SETTINGS="$CONFIG_DIR/settings.json"
ENV_FILE="$CONFIG_DIR/amx-langfuse.env"
# The command claude runs at Stop. `uv run --script` reads the inline
# dependency metadata in the hook and installs langfuse into an ephemeral env.
HOOK_CMD="uv run --script $DEST_HOOK"
# The danger hook has NO external dependencies (stdlib only), so it runs under
# plain python3 — no uv, no checksum (self-authored, not vendored). It fires on
# PreToolUse with a "Bash" matcher.
DANGER_HOOK_CMD="python3 $DEST_DANGER_HOOK"
# The session cost-structure hook is also stdlib-only, so it runs under plain
# python3 as well. It fires on Stop, alongside the langfuse tracer: settings.json
# keeps one entry per exact command string, so both run on the same event. It is
# installed unconditionally because it stays inert until AMX_SESSION_INGEST_URL and
# AMX_SESSION_INGEST_TOKEN are set, and Stop fires once per session (unlike the
# danger hook's PreToolUse, which sits on every Bash call and is therefore opt-in).
SESSION_HOOK_CMD="python3 $DEST_SESSION_HOOK"

# ---- settings.json merge/removal (idempotent, preserves other keys) ---------
# python reads the settings file BY PATH (never via stdin — stdin carries the
# script here) so all sibling keys/hooks are preserved. Adds the entry only when
# absent; --uninstall removes matching entries. Writes atomically and prints
# CHANGED / UNCHANGED so the shell reports without a second write.
#
# Args: <mode> <event> <matcher> <cmd>. `event` is Stop or PreToolUse; `matcher`
# is the tool-name filter for PreToolUse ("" = no matcher key, as Stop uses).
# Identity of "our" block is the exact command string, so Stop and PreToolUse
# entries never collide and unrelated sibling hooks are untouched.
apply_settings() {
	_mode="$1"; _event="$2"; _matcher="$3"; _cmd="$4"
	_res=$(python3 - "$_mode" "$_event" "$_matcher" "$_cmd" "$SETTINGS" <<'PY'
import copy, json, os, sys

mode, event, matcher, cmd, path = sys.argv[1:6]
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
entries = hooks.get(event)
if not isinstance(entries, list):
    entries = []

def has_cmd(block):
    return any(isinstance(h, dict) and h.get("type") == "command" and h.get("command") == cmd
              for h in block.get("hooks", []))

if mode == "install":
    if not any(isinstance(b, dict) and has_cmd(b) for b in entries):
        block = {"hooks": [{"type": "command", "command": cmd}]}
        if matcher:
            block = {"matcher": matcher, "hooks": block["hooks"]}
        entries.append(block)
    hooks[event] = entries
    data["hooks"] = hooks
else:  # uninstall
    new_entries = []
    for b in entries:
        if not isinstance(b, dict):
            new_entries.append(b); continue
        kept = [h for h in b.get("hooks", [])
                if not (isinstance(h, dict) and h.get("type") == "command" and h.get("command") == cmd)]
        if kept:
            nb = dict(b); nb["hooks"] = kept; new_entries.append(nb)
        # a block whose only hook was ours is dropped entirely
    if new_entries:
        hooks[event] = new_entries
    else:
        hooks.pop(event, None)
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
		UNCHANGED) info "settings.json already up to date ($_event, no change)" ;;
		CHANGED)   info "settings.json updated ($_mode $_event hook)" ;;
		*)         die "unexpected settings merge result: $_res" ;;
	esac
}

if [ "$UNINSTALL" = 1 ]; then
	[ -d "$CONFIG_DIR" ] || die "config dir not found: $CONFIG_DIR"
	apply_settings uninstall Stop "" "$HOOK_CMD"
	apply_settings uninstall Stop "" "$SESSION_HOOK_CMD"
	# Always remove the danger PreToolUse entry too (regardless of the flag), so
	# a single --uninstall fully reverses either install shape.
	apply_settings uninstall PreToolUse Bash "$DANGER_HOOK_CMD"
	if [ -f "$ENV_FILE" ]; then
		rm -f "$ENV_FILE" && info "removed $ENV_FILE"
	else
		info "no env file to remove ($ENV_FILE)"
	fi
	info "left hook scripts in place: $DEST_HOOK, $DEST_DANGER_HOOK, $DEST_SESSION_HOOK (safe; inert without env/config)"
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
# Args: <file> <recorded-name>. Verifies one SHA256SUMS entry. python3 (already
# required) does the hashing, so we do not depend on sha256sum(1) being present.
verify_sum() {
	python3 - "$1" "$SUMS_FILE" "$2" <<'SUMPY' || die "checksum verification failed for $2 - refusing to install"
import hashlib, sys
src, sums, name = sys.argv[1], sys.argv[2], sys.argv[3]
want = ""
for line in open(sums):
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    h, _, entry = line.partition("  ")
    if entry.strip() == name:
        want = h.strip().lower()
        break
if not want:
    sys.stderr.write("no %s entry in SHA256SUMS\n" % name); sys.exit(1)
got = hashlib.sha256(open(src, "rb").read()).hexdigest()
if got != want:
    sys.stderr.write("checksum mismatch for %s: expected %s got %s\n" % (name, want, got)); sys.exit(1)
SUMPY
}
verify_sum "$SRC_HOOK" langfuse_hook.py
[ -f "$SRC_SESSION_HOOK" ] || die "session usage hook not found: $SRC_SESSION_HOOK"
verify_sum "$SRC_SESSION_HOOK" session_usage_hook.py
info "verified hook checksums"

# 'uv' is mandatory: the Stop hook runs via `uv run --script`, which reads the
# hook's inline dependency metadata and installs langfuse into an ephemeral env.
command -v uv >/dev/null 2>&1 || die "'uv' is required on PATH (Stop hook runs 'uv run --script'); install from https://docs.astral.sh/uv/"

mkdir -p "$HOOKS_DIR" || die "cannot create $HOOKS_DIR"
cp "$SRC_HOOK" "$DEST_HOOK" || die "cannot copy hook to $DEST_HOOK"
info "installed hook: $DEST_HOOK"

apply_settings install Stop "" "$HOOK_CMD"

# ---- session cost-structure hook (always installed, inert without env) -------
# Self-authored, stdlib-only. Reads only message.usage out of the session
# transcript and posts model-keyed token aggregates; never prompt/response text.
cp "$SRC_SESSION_HOOK" "$DEST_SESSION_HOOK" || die "cannot copy session hook to $DEST_SESSION_HOOK"
info "installed session usage hook: $DEST_SESSION_HOOK"
apply_settings install Stop "" "$SESSION_HOOK_CMD"

# ---- danger-command detection hook (opt-in) ---------------------------------
# Self-authored, stdlib-only; copied and wired into PreToolUse. It is inert
# until AMX_DANGER_INGEST_URL/TOKEN are set (see amx-langfuse.env below).
if [ "$WITH_DANGER" = 1 ]; then
	[ -f "$SRC_DANGER_HOOK" ] || die "danger hook not found: $SRC_DANGER_HOOK"
	cp "$SRC_DANGER_HOOK" "$DEST_DANGER_HOOK" || die "cannot copy danger hook to $DEST_DANGER_HOOK"
	info "installed danger hook: $DEST_DANGER_HOOK"
	apply_settings install PreToolUse Bash "$DANGER_HOOK_CMD"
fi

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
#
# ---- Danger-command detection (PreToolUse danger_hook.py) --------------------
# Set BOTH to arm the danger hook; leave either unset and the hook is a no-op.
# The hook posts a masked digest (never the raw command) to the AMS ingest
# endpoint. Point the URL at your AMS and use the server's danger_ingest_token.
# AMX_DANGER_INGEST_URL='http://ams-host:8080/api/v1/ingest/danger-command'
# AMX_DANGER_INGEST_TOKEN='<must match AMS settings.danger_ingest_token>'
# Optional extra regex patterns (one per line, file MUST be mode 0600):
# CC_DANGER_PATTERNS_FILE=
#
# ---- Session cost structure (Stop session_usage_hook.py) ---------------------
# Set BOTH to arm the session hook; leave either unset and the hook is a no-op.
# It posts per-model token aggregates (1h/5m cache writes kept apart, thinking
# tokens, service-tier and stop-reason counts) - never prompt or response text.
# The token is SEPARATE from the danger one: this path only upserts diagnostic
# rows, so a host may be armed for it without being able to open alerts.
# AMX_SESSION_INGEST_URL='http://ams-host:8080/api/v1/ingest/session-usage'
# AMX_SESSION_INGEST_TOKEN='<must match AMS settings.session_ingest_token>'
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
