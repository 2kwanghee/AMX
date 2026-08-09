#!/usr/bin/env bash
#
# make-ca.sh — create a self-signed internal CA for AMX TLS (B4 / §7 in-transit).
#
# Produces ca.key + ca.crt. The CA cert (ca.crt) is what AMA trusts via
# AMX_AMS_TLS_CA; ca.key signs server/client certs via issue-cert.sh and must
# never leave the issuing host. EC P-256 keys, matching docs/DEPLOYMENT-TLS.md §3.
#
# Not idempotent by design: if ca.key or ca.crt already exists we fail loud
# rather than overwrite a CA (silently replacing it would orphan every cert it
# ever signed and every AMA that trusts it).
#
# Usage:
#   deploy/tls/make-ca.sh [--cn NAME] [--days N] [--out DIR]
#
#   --cn    NAME   CA subject common name           (default: amx-internal-ca)
#   --days  N      CA validity in days              (default: 3650)
#   --out   DIR    output directory                 (default: current dir)
#
# Env overrides (flags win): CA_CN, CA_DAYS.
set -euo pipefail

CA_CN="${CA_CN:-amx-internal-ca}"
CA_DAYS="${CA_DAYS:-3650}"
OUT_DIR="."

die() { echo "make-ca.sh: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --cn)   CA_CN="${2:?--cn needs a value}"; shift 2 ;;
    --days) CA_DAYS="${2:?--days needs a value}"; shift 2 ;;
    --out)  OUT_DIR="${2:?--out needs a value}"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

command -v openssl >/dev/null 2>&1 || die "openssl not found on PATH"
case "$CA_DAYS" in ''|*[!0-9]*) die "--days must be an integer, got: $CA_DAYS" ;; esac

mkdir -p "$OUT_DIR"
CA_KEY="$OUT_DIR/ca.key"
CA_CRT="$OUT_DIR/ca.crt"

# fail-loud: refuse to clobber an existing CA.
[ -e "$CA_KEY" ] && die "$CA_KEY already exists — refusing to overwrite an existing CA"
[ -e "$CA_CRT" ] && die "$CA_CRT already exists — refusing to overwrite an existing CA"

umask 077
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
  -keyout "$CA_KEY" -out "$CA_CRT" -days "$CA_DAYS" \
  -subj "/CN=$CA_CN" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
  -addext "keyUsage=critical,keyCertSign,cRLSign"

chmod 600 "$CA_KEY"
chmod 644 "$CA_CRT"

echo "make-ca.sh: created CA"
echo "  key : $CA_KEY   (secret — signs certs, never distribute)"
echo "  cert: $CA_CRT   (distribute to AMA as AMX_AMS_TLS_CA)"
echo "  CN=$CA_CN  valid ${CA_DAYS}d"
