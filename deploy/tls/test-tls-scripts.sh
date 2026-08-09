#!/usr/bin/env bash
#
# test-tls-scripts.sh — self-test for the B4 TLS issuance scripts.
#
# Runs entirely in a temp dir with openssl only: no root, no network, no sockets.
# Exercises make-ca.sh -> issue-cert.sh (server + client) -> chain verify -> SAN
# and EKU assertions -> fail-loud-on-existing behaviour.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
MAKE_CA="$HERE/make-ca.sh"
ISSUE="$HERE/issue-cert.sh"

pass=0; fail=0
ok()   { echo "  PASS: $*"; pass=$((pass+1)); }
bad()  { echo "  FAIL: $*"; fail=$((fail+1)); }

command -v openssl >/dev/null 2>&1 || { echo "openssl not found on PATH" >&2; exit 2; }
[ -x "$MAKE_CA" ] || chmod +x "$MAKE_CA" 2>/dev/null || true
[ -x "$ISSUE" ]   || chmod +x "$ISSUE" 2>/dev/null || true

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
echo "test-tls-scripts.sh: work dir $WORK"

# 1. make CA -------------------------------------------------------------------
echo "[1] make-ca.sh"
bash "$MAKE_CA" --cn amx-test-ca --days 3650 --out "$WORK" >/dev/null
[ -s "$WORK/ca.key" ] && [ -s "$WORK/ca.crt" ] && ok "CA key+cert created" || bad "CA files missing"
openssl x509 -in "$WORK/ca.crt" -noout -ext basicConstraints 2>/dev/null | grep -q "CA:TRUE" \
  && ok "CA cert is a CA (basicConstraints CA:TRUE)" || bad "CA cert not marked CA:TRUE"

# 2. issue server cert with DNS + IP SAN --------------------------------------
echo "[2] issue-cert.sh (server, DNS+IP SAN)"
bash "$ISSUE" --cn ams.internal --dns ams.internal --dns ams.local --ip 10.0.0.10 \
  --days 365 --name server --ca-cert "$WORK/ca.crt" --ca-key "$WORK/ca.key" \
  --out "$WORK" >/dev/null
[ -s "$WORK/server.key" ] && [ -s "$WORK/server.crt" ] && ok "server key+cert created" || bad "server files missing"

# 3. issue client cert (mTLS) --------------------------------------------------
echo "[3] issue-cert.sh --client (mTLS)"
bash "$ISSUE" --client --cn amx-agent-01 --days 365 --name client \
  --ca-cert "$WORK/ca.crt" --ca-key "$WORK/ca.key" --out "$WORK" >/dev/null
[ -s "$WORK/client.key" ] && [ -s "$WORK/client.crt" ] && ok "client key+cert created" || bad "client files missing"

# 4. verify chains against the CA ---------------------------------------------
echo "[4] openssl verify (chain)"
openssl verify -CAfile "$WORK/ca.crt" "$WORK/server.crt" >/dev/null 2>&1 \
  && ok "server cert verifies against CA" || bad "server cert failed chain verify"
openssl verify -CAfile "$WORK/ca.crt" "$WORK/client.crt" >/dev/null 2>&1 \
  && ok "client cert verifies against CA" || bad "client cert failed chain verify"

# 5. SAN present on server cert -----------------------------------------------
echo "[5] SAN assertions"
san=$(openssl x509 -in "$WORK/server.crt" -noout -ext subjectAltName 2>/dev/null || true)
echo "$san" | grep -q "DNS:ams.internal" && ok "server SAN has DNS:ams.internal" || bad "missing DNS:ams.internal SAN"
echo "$san" | grep -q "DNS:ams.local"    && ok "server SAN has DNS:ams.local"    || bad "missing DNS:ams.local SAN"
echo "$san" | grep -q "IP Address:10.0.0.10" && ok "server SAN has IP 10.0.0.10" || bad "missing IP:10.0.0.10 SAN"

# 6. EKU: server=serverAuth, client=clientAuth --------------------------------
echo "[6] extendedKeyUsage assertions"
seku=$(openssl x509 -in "$WORK/server.crt" -noout -ext extendedKeyUsage 2>/dev/null || true)
echo "$seku" | grep -q "TLS Web Server Authentication" && ok "server EKU=serverAuth" || bad "server EKU not serverAuth"
ceku=$(openssl x509 -in "$WORK/client.crt" -noout -ext extendedKeyUsage 2>/dev/null || true)
echo "$ceku" | grep -q "TLS Web Client Authentication" && ok "client EKU=clientAuth" || bad "client EKU not clientAuth"

# 7. fail-loud: re-running make-ca over an existing CA must fail ----------------
echo "[7] fail-loud on existing files"
if bash "$MAKE_CA" --out "$WORK" >/dev/null 2>&1; then
  bad "make-ca.sh overwrote an existing CA (should have failed)"
else
  ok "make-ca.sh refused to overwrite existing CA"
fi
if bash "$ISSUE" --cn ams.internal --dns ams.internal --name server \
     --ca-cert "$WORK/ca.crt" --ca-key "$WORK/ca.key" --out "$WORK" >/dev/null 2>&1; then
  bad "issue-cert.sh overwrote an existing cert (should have failed)"
else
  ok "issue-cert.sh refused to overwrite existing cert"
fi

# 8. fail-loud: server cert without a SAN is rejected --------------------------
echo "[8] server cert requires a SAN"
if bash "$ISSUE" --cn no-san --name nosan \
     --ca-cert "$WORK/ca.crt" --ca-key "$WORK/ca.key" --out "$WORK" >/dev/null 2>&1; then
  bad "issue-cert.sh issued a server cert with no SAN (should have failed)"
else
  ok "issue-cert.sh refused a server cert with no SAN"
fi

echo
echo "test-tls-scripts.sh: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
