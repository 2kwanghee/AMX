#!/usr/bin/env bash
#
# issue-cert.sh — issue an AMS server (or, with --client, an mTLS client) cert
# signed by the internal CA from make-ca.sh (B4 / §7 in-transit).
#
# Server certs get extendedKeyUsage=serverAuth and REQUIRE at least one SAN
# entry (DNS or IP) — AMA verifies the SAN against the host it dials, so a cert
# with no SAN fails verification (docs/DEPLOYMENT-TLS.md §3). Client certs get
# extendedKeyUsage=clientAuth for mTLS (AMS with AMX_GRPC_TLS_CA requires them).
#
# Not idempotent by design: existing <name>.key / <name>.crt cause a loud
# failure rather than an overwrite.
#
# Usage:
#   deploy/tls/issue-cert.sh --cn HOST [--dns NAME]... [--ip ADDR]... \
#                            [--client] [--days N] [--name PREFIX] \
#                            [--ca-cert PATH] [--ca-key PATH] [--out DIR]
#
#   --cn      HOST   subject common name (server: the AMS hostname)   (required)
#   --dns     NAME   add a DNS SAN entry            (repeatable)
#   --ip      ADDR   add an IP SAN entry            (repeatable)
#   --client         issue a CLIENT cert (clientAuth) for mTLS instead of server
#   --days    N      cert validity in days                    (default: 365)
#   --name    PREFIX output basename <PREFIX>.key/.crt/.csr
#                    (default: "server", or "client" with --client)
#   --ca-cert PATH   CA certificate                           (default: ca.crt)
#   --ca-key  PATH   CA private key                           (default: ca.key)
#   --out     DIR    output directory                         (default: current)
#
# Env overrides (flags win): CERT_DAYS.
#
# For a server cert, pass the SAN AMA will dial, e.g.:
#   issue-cert.sh --cn ams.internal --dns ams.internal --ip 10.0.0.10
# For an mTLS client cert:
#   issue-cert.sh --client --cn amx-agent-01
set -euo pipefail

CN=""
DAYS="${CERT_DAYS:-365}"
NAME=""
IS_CLIENT=0
CA_CRT="ca.crt"
CA_KEY="ca.key"
OUT_DIR="."
DNS_SANS=()
IP_SANS=()

die() { echo "issue-cert.sh: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --cn)      CN="${2:?--cn needs a value}"; shift 2 ;;
    --dns)     DNS_SANS+=("${2:?--dns needs a value}"); shift 2 ;;
    --ip)      IP_SANS+=("${2:?--ip needs a value}"); shift 2 ;;
    --client)  IS_CLIENT=1; shift ;;
    --days)    DAYS="${2:?--days needs a value}"; shift 2 ;;
    --name)    NAME="${2:?--name needs a value}"; shift 2 ;;
    --ca-cert) CA_CRT="${2:?--ca-cert needs a value}"; shift 2 ;;
    --ca-key)  CA_KEY="${2:?--ca-key needs a value}"; shift 2 ;;
    --out)     OUT_DIR="${2:?--out needs a value}"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

command -v openssl >/dev/null 2>&1 || die "openssl not found on PATH"
[ -n "$CN" ] || die "--cn is required"
case "$DAYS" in ''|*[!0-9]*) die "--days must be an integer, got: $DAYS" ;; esac
[ -e "$CA_CRT" ] || die "CA cert not found: $CA_CRT (run make-ca.sh first)"
[ -e "$CA_KEY" ] || die "CA key not found: $CA_KEY (run make-ca.sh first)"

if [ "$IS_CLIENT" -eq 1 ]; then
  EKU="clientAuth"
  [ -n "$NAME" ] || NAME="client"
else
  EKU="serverAuth"
  [ -n "$NAME" ] || NAME="server"
  # A server cert with no SAN fails AMA verification — refuse to issue one.
  if [ "${#DNS_SANS[@]}" -eq 0 ] && [ "${#IP_SANS[@]}" -eq 0 ]; then
    die "a server cert needs at least one --dns or --ip SAN (AMA verifies it)"
  fi
fi

mkdir -p "$OUT_DIR"
KEY="$OUT_DIR/$NAME.key"
CSR="$OUT_DIR/$NAME.csr"
CRT="$OUT_DIR/$NAME.crt"

# fail-loud: never overwrite existing key/cert material.
[ -e "$KEY" ] && die "$KEY already exists — refusing to overwrite"
[ -e "$CRT" ] && die "$CRT already exists — refusing to overwrite"

# Build the subjectAltName list.
san_parts=()
for d in "${DNS_SANS[@]:-}"; do [ -n "$d" ] && san_parts+=("DNS:$d"); done
for i in "${IP_SANS[@]:-}"; do [ -n "$i" ] && san_parts+=("IP:$i"); done
SAN=""
if [ "${#san_parts[@]}" -gt 0 ]; then
  SAN=$(IFS=,; echo "${san_parts[*]}")
fi

EXT_FILE="$OUT_DIR/.$NAME.ext.$$"
cleanup() { rm -f "$CSR" "$EXT_FILE"; }
trap cleanup EXIT

{
  echo "basicConstraints=CA:FALSE"
  echo "keyUsage=critical,digitalSignature"
  echo "extendedKeyUsage=$EKU"
  [ -n "$SAN" ] && echo "subjectAltName=$SAN"
} > "$EXT_FILE"

umask 077
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
  -keyout "$KEY" -out "$CSR" -subj "/CN=$CN"

openssl x509 -req -in "$CSR" -CA "$CA_CRT" -CAkey "$CA_KEY" -CAcreateserial \
  -out "$CRT" -days "$DAYS" -extfile "$EXT_FILE"

chmod 600 "$KEY"
chmod 644 "$CRT"

kind="server"; [ "$IS_CLIENT" -eq 1 ] && kind="client"
echo "issue-cert.sh: issued $kind cert"
echo "  key : $KEY"
echo "  cert: $CRT"
echo "  CN=$CN  EKU=$EKU  valid ${DAYS}d${SAN:+  SAN=$SAN}"
if [ "$IS_CLIENT" -eq 1 ]; then
  echo "  -> AMA mTLS: AMX_AMS_TLS_CLIENT_CERT=$CRT  AMX_AMS_TLS_CLIENT_KEY=$KEY"
else
  echo "  -> AMS: AMX_GRPC_TLS_CERT=$CRT  AMX_GRPC_TLS_KEY=$KEY"
fi
