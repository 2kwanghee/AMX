#!/usr/bin/env bash
#
# verify-tls.sh — smoke-test issued TLS artifacts (B4 완료조건).
#
# (a) TLS-handshake check: confirm an AMS server cert + key negotiate TLS and
#     verify against the CA. Two modes:
#       - default (self-contained): spin up a loopback `openssl s_server` with
#         the issued cert and connect with `openssl s_client -verify_return_error`.
#       - --endpoint HOST:PORT: probe a *running* AMS instead (no local server).
#
# (b) fail-closed check: confirm AMS refuses to start without TLS when
#     AMX_GRPC_ALLOW_INSECURE is unset and no cert/key is provided. This exercises
#     the real ams-server code (configure_port). Running the full AMS is heavy, so
#     this is a configure_port UNIT check (documented alternative in the B4 spec).
#     Skipped with a clear notice if the ams-server Python env is not importable.
#
# Usage:
#   deploy/tls/verify-tls.sh [--ca PATH] [--cert PATH] [--key PATH]
#                            [--server-name NAME] [--endpoint HOST:PORT]
#                            [--ams-dir DIR] [--skip-failclosed]
#
#   --ca          PATH        CA cert to verify against          (default: ca.crt)
#   --cert        PATH        server cert                        (default: server.crt)
#   --key         PATH        server key                         (default: server.key)
#   --server-name NAME        SNI / cert name to verify          (default: from CN/SAN)
#   --endpoint    HOST:PORT   probe a running AMS instead of a local s_server
#   --ams-dir     DIR         ams-server dir for the fail-closed check
#                             (default: <repo>/ams-server)
#   --skip-failclosed         skip check (b)
set -euo pipefail

CA="ca.crt"
CERT="server.crt"
KEY="server.key"
SERVER_NAME=""
ENDPOINT=""
AMS_DIR=""
SKIP_FAILCLOSED=0

die() { echo "verify-tls.sh: $*" >&2; exit 1; }
info() { echo "verify-tls.sh: $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --ca)             CA="${2:?}"; shift 2 ;;
    --cert)           CERT="${2:?}"; shift 2 ;;
    --key)            KEY="${2:?}"; shift 2 ;;
    --server-name)    SERVER_NAME="${2:?}"; shift 2 ;;
    --endpoint)       ENDPOINT="${2:?}"; shift 2 ;;
    --ams-dir)        AMS_DIR="${2:?}"; shift 2 ;;
    --skip-failclosed) SKIP_FAILCLOSED=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

command -v openssl >/dev/null 2>&1 || die "openssl not found on PATH"
[ -e "$CA" ] || die "CA not found: $CA"

# ---------------------------------------------------------------------------
# (a) TLS handshake
# ---------------------------------------------------------------------------
if [ -n "$ENDPOINT" ]; then
  info "(a) probing running AMS at $ENDPOINT ..."
  host="${ENDPOINT%%:*}"
  sni="${SERVER_NAME:-$host}"
  out=$(echo Q | openssl s_client -connect "$ENDPOINT" -servername "$sni" \
        -CAfile "$CA" -verify_return_error -verify 4 2>&1) \
    || die "TLS handshake/verify against $ENDPOINT failed:\n$out"
  echo "$out" | grep -q "Verify return code: 0 (ok)" \
    || die "endpoint did not verify against $CA:\n$out"
  info "(a) PASS — TLS established and verified against $CA"
else
  [ -e "$CERT" ] || die "server cert not found: $CERT"
  [ -e "$KEY" ] || die "server key not found: $KEY"
  info "(a) loopback handshake with $CERT ..."
  # Derive an SNI to verify: explicit flag, else first DNS SAN, else CN.
  sni="$SERVER_NAME"
  if [ -z "$sni" ]; then
    sni=$(openssl x509 -in "$CERT" -noout -ext subjectAltName 2>/dev/null \
          | grep -oE 'DNS:[^,]+' | head -1 | cut -d: -f2 || true)
  fi
  [ -n "$sni" ] || sni=$(openssl x509 -in "$CERT" -noout -subject 2>/dev/null \
                          | sed -n 's/.*CN *= *\([^,]*\).*/\1/p')
  [ -n "$sni" ] || die "could not determine a server name from $CERT; pass --server-name"

  # Pick a free loopback port and run s_server briefly.
  port=0
  if command -v python3 >/dev/null 2>&1; then
    port=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
  else
    port=$(( (RANDOM % 20000) + 20000 ))
  fi

  srv_log=$(mktemp)
  cli_log=$(mktemp)
  openssl s_server -accept "127.0.0.1:$port" -cert "$CERT" -key "$KEY" \
    -quiet >"$srv_log" 2>&1 &
  srv_pid=$!
  cleanup_a() { kill "$srv_pid" 2>/dev/null || true; rm -f "$srv_log" "$cli_log"; }
  trap cleanup_a EXIT

  # Wait for s_server to bind the port (don't open a probe connection — that
  # would consume the handshake we're about to test). Falls back to a fixed
  # nap when `ss` is unavailable.
  bound=0
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if command -v ss >/dev/null 2>&1; then
      if ss -ltn 2>/dev/null | grep -q ":$port\b"; then bound=1; break; fi
    else
      sleep 0.5; bound=1; break
    fi
    sleep 0.2
  done
  [ "$bound" -eq 1 ] || die "openssl s_server did not bind 127.0.0.1:$port:\n$(cat "$srv_log")"

  echo Q | openssl s_client -connect "127.0.0.1:$port" -servername "$sni" \
      -CAfile "$CA" -verify_return_error -verify 4 >"$cli_log" 2>&1 \
    || die "loopback TLS handshake/verify failed (sni=$sni):\n$(cat "$cli_log")"
  grep -q "Verify return code: 0 (ok)" "$cli_log" \
    || die "server cert did not verify against $CA (sni=$sni):\n$(cat "$cli_log")"
  info "(a) PASS — issued cert negotiates TLS and verifies against $CA (sni=$sni)"
  cleanup_a
  trap - EXIT
fi

# ---------------------------------------------------------------------------
# (b) fail-closed: AMS refuses plaintext start without opt-in
# ---------------------------------------------------------------------------
if [ "$SKIP_FAILCLOSED" -eq 1 ]; then
  info "(b) SKIPPED (--skip-failclosed)"
  exit 0
fi

if [ -z "$AMS_DIR" ]; then
  script_dir=$(cd "$(dirname "$0")" && pwd)
  AMS_DIR="$script_dir/../../ams-server"
fi

if [ ! -d "$AMS_DIR/app" ]; then
  info "(b) SKIPPED — ams-server not found at $AMS_DIR (pass --ams-dir)"
  exit 0
fi

PYBIN="python3"; command -v python3 >/dev/null 2>&1 || PYBIN="python"
if ! "$PYBIN" -c 'import grpc' >/dev/null 2>&1; then
  info "(b) SKIPPED — Python grpc not importable; run inside the ams-server env to exercise this"
  exit 0
fi

info "(b) configure_port fail-closed unit check (ams-server) ..."
( cd "$AMS_DIR" && env -u AMX_GRPC_TLS_CERT -u AMX_GRPC_TLS_KEY -u AMX_GRPC_ALLOW_INSECURE \
  "$PYBIN" - <<'PY'
import sys
import grpc
from app.grpc.server import configure_port

srv = grpc.aio.server()
# no cert/key, no ALLOW_INSECURE -> must refuse to start (fail-closed).
try:
    configure_port(srv, 0)
except RuntimeError as e:
    if "AMX_GRPC_ALLOW_INSECURE" in str(e):
        print("verify-tls.sh: (b) PASS — AMS refuses to start without TLS or opt-in")
        sys.exit(0)
    print(f"verify-tls.sh: (b) FAIL — wrong error: {e}", file=sys.stderr)
    sys.exit(1)
print("verify-tls.sh: (b) FAIL — AMS started without TLS and without opt-in", file=sys.stderr)
sys.exit(1)
PY
) || die "(b) fail-closed check failed"

info "all checks passed"
