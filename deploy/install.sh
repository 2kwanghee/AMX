#!/usr/bin/env bash
# install.sh — 패키지형 설치 (대상 머신에 git·go·python 이 없어도 동작).
#
#   sha256(install.sh): <PR5 모달이 이 자리에 지문을 각인한다>
#
# 대역외로 받은 한 줄로 실행한다:
#   curl -fsSL http://HOST:8080/install.sh | bash -s -- \
#       --ams HOST:50051 --token <enroll> --pubkey <B64> --insecure
#
# 하는 일: 매니페스트(Ed25519 서명) 검증 → os/arch 바이너리·wheel 다운로드 →
#          sha256 대조 → ama 배치 + uv 부트스트랩 + tsamx 설치 → enroll·기동.
#
# 신뢰 경계: --insecure(기본)는 평문 HTTP 로 산출물을 받는다. 전송 자체는 MITM 에
#   노출되지만, 매니페스트가 대역외 --pubkey 로 서명 검증되고 모든 산출물이 그
#   매니페스트의 sha256 과 대조되므로, 변조된 바이너리는 설치 전에 걸러진다.
#   따라서 평문이라도 "신뢰 LAN 한정"에서 코드 무결성은 유지된다. 유출(토큰 등
#   요청 헤더)은 별개 문제이니 신뢰 LAN 밖에서는 --ca 로 TLS 를 쓰라.
#
# 의존성: curl · openssl(3.0+, Ed25519 -rawin) · jq · sha256sum(또는 shasum).
set -euo pipefail

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_dim=$'\033[2m'; c_rst=$'\033[0m'
ok()   { printf '%s✔%s %s\n' "$c_grn" "$c_rst" "$*"; }
warn() { printf '%s!%s %s\n' "$c_yel" "$c_rst" "$*" >&2; }
err()  { printf '%sx%s %s\n' "$c_red" "$c_rst" "$*" >&2; }
die()  { err "$*"; exit 1; }
step() { printf '%s· %s%s\n' "$c_dim" "$*" "$c_rst"; }

# ── 인자 ───────────────────────────────────────────────────────────────────────
AMS_ADDR=""; TOKEN=""; PUBKEY=""; AMS_URL=""; TLS_CA=""
INSECURE=0; DRY_RUN=0
CONFIG_DIR="$HOME/.claude-amx"
AGENT_ID="ama_dev"
INSTALL_ROOT="$HOME/.amx"

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --ams)        AMS_ADDR="$2"; shift 2 ;;
    --token)      TOKEN="$2"; shift 2 ;;
    --pubkey)     PUBKEY="$2"; shift 2 ;;
    --ams-url)    AMS_URL="$2"; shift 2 ;;
    --ca)         TLS_CA="$2"; shift 2 ;;
    --insecure)   INSECURE=1; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --config-dir) CONFIG_DIR="$2"; shift 2 ;;
    --agent-id)   AGENT_ID="$2"; shift 2 ;;
    --install-root) INSTALL_ROOT="$2"; shift 2 ;;
    -h|--help)    usage; exit 0 ;;
    *) die "알 수 없는 인자: $1 (--help 로 사용법)" ;;
  esac
done

# ── 필수 인자 검증 (fail-loud, 네트워크 접촉 전) ───────────────────────────────
[ -n "$AMS_ADDR" ] || die "--ams HOST:PORT 가 필요합니다 (gRPC 제어면, 예: 10.60.1.15:50051)"
[ -n "$PUBKEY" ]   || die "--pubkey <B64> 가 필요합니다 (AMS 서명 공개키, 대역외로 전달받은 값)"
if [ "$INSECURE" = 1 ]; then
  [ -z "$TLS_CA" ] || die "--insecure 와 --ca 는 함께 쓸 수 없습니다"
else
  [ -n "$TLS_CA" ] || die "보안 미설정: --ca <PEM> 로 TLS 를 쓰거나, 첫 시험이면 --insecure 를 주세요"
  [ -f "$TLS_CA" ] || die "--ca 파일을 찾을 수 없음: $TLS_CA"
fi
[ -n "$TOKEN" ] || warn "--token 이 없습니다 — 최초 enroll 이라면 기동이 실패합니다"

# 다운로드 베이스: --ams-url 우선, 없으면 --ams 의 호스트 + :8080 (REST 포트)로 유도.
if [ -z "$AMS_URL" ]; then
  ams_host="${AMS_ADDR%%:*}"
  [ -n "$ams_host" ] || die "--ams 에서 호스트를 추출할 수 없습니다: $AMS_ADDR"
  scheme="https"; [ "$INSECURE" = 1 ] && scheme="http"
  AMS_URL="$scheme://$ams_host:8080"
fi
AMS_URL="${AMS_URL%/}"   # 뒤 슬래시 제거

# ── 의존성 점검 ────────────────────────────────────────────────────────────────
need() { command -v "$1" >/dev/null 2>&1 || die "필요한 명령이 없습니다: $1  ($2)"; }
need curl    "산출물 다운로드"
need openssl "매니페스트 Ed25519 서명 검증 (OpenSSL 3.0+ 필요)"
need jq      "매니페스트 JSON 파싱 (취약한 수동 파싱을 피하기 위해 요구)"
# sha256: sha256sum 우선, macOS 등에서는 shasum 폴백.
SHA_CMD=""
if command -v sha256sum >/dev/null 2>&1; then SHA_CMD="sha256sum"
elif command -v shasum >/dev/null 2>&1; then SHA_CMD="shasum -a 256"
else die "sha256 도구가 없습니다: sha256sum 또는 shasum 이 필요합니다"; fi
sha256_of() { $SHA_CMD "$1" | cut -d' ' -f1; }

# ── os/arch 감지 ───────────────────────────────────────────────────────────────
uname_s="$(uname -s)"; uname_m="$(uname -m)"
case "$uname_s" in
  Linux)  os="linux" ;;
  Darwin) die "macOS 는 아직 이 설치 경로를 지원하지 않습니다 (linux/windows 만)" ;;
  MINGW*|MSYS*|CYGWIN*) die "Windows 는 install.ps1 을 사용하세요 (irm ...|iex)" ;;
  *) die "지원하지 않는 OS: $uname_s" ;;
esac
case "$uname_m" in
  x86_64|amd64)        arch="amd64" ;;
  aarch64|arm64)       arch="arm64" ;;
  *) die "지원하지 않는 아키텍처: $uname_m (amd64/arm64 만)" ;;
esac
BIN_NAME="ama-$os-$arch"
ok "대상: $os-$arch → 바이너리 $BIN_NAME"

# ── 작업 디렉터리 ──────────────────────────────────────────────────────────────
WORK="$(mktemp -d "${TMPDIR:-/tmp}/amx-install.XXXXXX")"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

CURL=(curl -fsSL)
[ "$INSECURE" != 1 ] && [ -n "$TLS_CA" ] && CURL+=(--cacert "$TLS_CA")

# ── 1) 매니페스트 획득 + Ed25519 서명 검증 ─────────────────────────────────────
step "매니페스트 다운로드: $AMS_URL/download/manifest.json"
"${CURL[@]}" "$AMS_URL/download/manifest.json" -o "$WORK/envelope.json" \
  || die "매니페스트를 받을 수 없습니다: $AMS_URL/download/manifest.json"

jq -e '.manifest and .signature and .algorithm' "$WORK/envelope.json" >/dev/null \
  || die "매니페스트 봉투 형식이 올바르지 않습니다 (manifest/signature/algorithm 필요)"
alg="$(jq -r '.algorithm' "$WORK/envelope.json")"
[ "$alg" = "ed25519:amx-manifest-v1" ] || die "알 수 없는 서명 알고리즘: $alg"

# 원문 바이트 보존이 핵심: 서명은 파일 바이트 그대로를 덮는다. jq -j 로 후행 개행
# 없이 추출하고, $() 캡처(후행 개행 삭제)를 절대 쓰지 않는다.
jq -j '.manifest' "$WORK/envelope.json" > "$WORK/manifest.txt"
jq -r '.signature' "$WORK/envelope.json" | base64 -d > "$WORK/sig.bin" \
  || die "서명(base64) 디코드 실패"

# 공개키(표준 base64 raw 32B) → openssl 이 먹는 DER SubjectPublicKeyInfo 로 래핑.
#   30 2a  30 05 06 03 2b 65 70  03 21 00  <32B>   (Ed25519 OID 1.3.101.112)
if ! printf '%s' "$PUBKEY" | base64 -d > "$WORK/pub.raw" 2>/dev/null; then
  die "--pubkey 를 base64 디코드할 수 없습니다"
fi
pub_len="$(wc -c < "$WORK/pub.raw" | tr -d ' ')"
[ "$pub_len" = 32 ] || die "--pubkey 가 32바이트 raw Ed25519 키가 아닙니다 (디코드 결과 ${pub_len}B)"
{ printf '\x30\x2a\x30\x05\x06\x03\x2b\x65\x70\x03\x21\x00'; cat "$WORK/pub.raw"; } > "$WORK/pub.der"

# 서명 대상 메시지 = 도메인 접두사 b"amx-manifest-v1\x00" + 매니페스트 원문 바이트.
{ printf 'amx-manifest-v1'; printf '\x00'; cat "$WORK/manifest.txt"; } > "$WORK/msg.bin"

step "Ed25519 서명 검증 (openssl)"
if ! openssl pkeyutl -verify -pubin -inkey "$WORK/pub.der" -keyform DER \
       -rawin -in "$WORK/msg.bin" -sigfile "$WORK/sig.bin" >/dev/null 2>&1; then
  die "매니페스트 서명 검증 실패 — 공개키 불일치이거나 매니페스트가 변조되었습니다.
    (openssl 3.0+ 의 Ed25519 -rawin 이 필요합니다: openssl version 확인)"
fi
ok "매니페스트 서명 검증 통과"

# ── 2) 매니페스트에서 대상 산출물 지목 ─────────────────────────────────────────
WHEEL_NAME="$(jq -r '.version.wheel // empty' "$WORK/manifest.txt")"
[ -n "$WHEEL_NAME" ] || die "매니페스트에 version.wheel 이 없습니다"
COMMIT="$(jq -r '.version.commit // "?"' "$WORK/manifest.txt")"

manifest_sha() {  # $1=name → 그 산출물의 기록된 sha256 (없으면 빈 문자열)
  jq -r --arg n "$1" '.artifacts[$n].sha256 // empty' "$WORK/manifest.txt"
}
BIN_SHA="$(manifest_sha "$BIN_NAME")"
WHEEL_SHA="$(manifest_sha "$WHEEL_NAME")"
[ -n "$BIN_SHA" ]   || die "매니페스트에 $BIN_NAME 항목이 없습니다 (이 os/arch 미빌드?)"
[ -n "$WHEEL_SHA" ] || die "매니페스트에 wheel($WHEEL_NAME) 항목이 없습니다"
ok "매니페스트 커밋 $COMMIT · wheel $WHEEL_NAME"

# ── 3) 다운로드 + sha256 대조 ──────────────────────────────────────────────────
# 서명 밖 심링크(tsamx-latest.whl)는 절대 받지 않는다 — 매니페스트가 지목한
# 실파일명만 받아 대조한다.
fetch_verify() {  # $1=name $2=want_sha → $WORK/$1 에 저장, sha 불일치면 die
  local name="$1" want="$2"
  step "다운로드: $name"
  "${CURL[@]}" "$AMS_URL/download/$name" -o "$WORK/$name" \
    || die "$name 다운로드 실패"
  local got; got="$(sha256_of "$WORK/$name")"
  [ "$got" = "$want" ] || die "sha256 불일치: $name (manifest=$want actual=$got)"
  ok "$name  sha256 일치"
}
fetch_verify "$BIN_NAME"   "$BIN_SHA"
fetch_verify "$WHEEL_NAME" "$WHEEL_SHA"

if [ "$DRY_RUN" = 1 ]; then
  printf '\n'
  ok "--dry-run: 서명 검증·다운로드·sha256 대조까지 통과. 설치·enroll 은 생략합니다."
  printf '%s  검증된 바이너리: %s%s\n' "$c_dim" "$WORK/$BIN_NAME (설치 예정 → $INSTALL_ROOT/ama)" "$c_rst"
  printf '%s  검증된 wheel   : %s%s\n' "$c_dim" "$WORK/$WHEEL_NAME (uv tool install 예정)" "$c_rst"
  exit 0
fi

# ── 4) 바이너리 배치 ───────────────────────────────────────────────────────────
STATE_DIR="$INSTALL_ROOT/state"
LOG_DIR="$INSTALL_ROOT/logs"
PIDFILE="$INSTALL_ROOT/ama.pid"
BIN="$INSTALL_ROOT/ama"
mkdir -p "$INSTALL_ROOT" "$STATE_DIR" "$LOG_DIR" "$CONFIG_DIR"
install -m 0755 "$WORK/$BIN_NAME" "$BIN"
ok "ama 배치 → $BIN"

# ── 5) uv 부트스트랩 + tsamx(wheel) 설치 ───────────────────────────────────────
if ! command -v uv >/dev/null 2>&1; then
  step "uv 부트스트랩"
  curl -LsSf https://astral.sh/uv/install.sh | sh \
    || die "uv 설치 실패 — https://astral.sh/uv 수동 설치 후 재시도"
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || die "uv 가 설치 후에도 PATH 에 없습니다 (~/.local/bin 확인)"
ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"

step "tsamx 설치 (uv tool install $WHEEL_NAME)"
uv tool install --python 3.12 "$WORK/$WHEEL_NAME" >/dev/null \
  || die "tsamx 설치 실패 — 'uv tool install --python 3.12 $WORK/$WHEEL_NAME' 출력 확인"
command -v tsamx >/dev/null 2>&1 || export PATH="$HOME/.local/bin:$PATH"
command -v tsamx >/dev/null 2>&1 || die "tsamx 가 PATH 에 없습니다 — ~/.local/bin 을 PATH 에 추가하세요"
TSAMX_BIN="$(command -v tsamx)"
ok "tsamx 설치 완료 ($TSAMX_BIN)"

# tsclaude 래퍼: 이 설치의 설정 홈을 각인해 tsamx 를 호출한다(agent-setup.sh 와 동일 관례).
WRAPPER_DIR="$HOME/.local/bin"; WRAPPER="$WRAPPER_DIR/tsclaude"
mkdir -p "$WRAPPER_DIR"
{
  printf '#!/usr/bin/env bash\n'
  printf '# AMX tsclaude 래퍼 — install.sh(패키지 설치) 시점에 각인됨.\n'
  printf 'exec env CLAUDE_CONFIG_DIR=%q %q "$@"\n' "$CONFIG_DIR" "$TSAMX_BIN"
} > "$WRAPPER"
chmod +x "$WRAPPER"
ok "tsclaude 래퍼 생성 ($WRAPPER → CLAUDE_CONFIG_DIR=$CONFIG_DIR)"

# ── 6) 설치 마커 (PR4 self_update 분기용) ──────────────────────────────────────
# 패키지 설치임을 기록한다. 저장소가 없으므로 self_update 는 git fast-forward 대신
# 이 베이스에서 재다운로드해야 한다(PR4). 토큰은 1회용이라 저장하지 않는다.
{
  printf '# install.sh(패키지 설치)가 기록한 마커. 토큰 제외.\n'
  printf 'AMX_INSTALL_METHOD=package\n'
  printf 'AMX_AMS_ADDR=%q\n' "$AMS_ADDR"
  printf 'AMX_AMS_URL=%q\n' "$AMS_URL"
  printf 'AMX_AMS_PUBKEY=%q\n' "$PUBKEY"
  printf 'AMX_AGENT_ID=%q\n' "$AGENT_ID"
  printf 'AMX_CONFIG_DIR=%q\n' "$CONFIG_DIR"
  printf 'AMX_INSTALLED_COMMIT=%q\n' "$COMMIT"
  printf 'AMX_INSECURE=%q\n' "$INSECURE"
  [ -n "$TLS_CA" ] && printf 'AMX_AMS_TLS_CA=%q\n' "$TLS_CA"
} > "$INSTALL_ROOT/install.env"
ok "설치 마커 기록 → $INSTALL_ROOT/install.env (install_method=package)"

# ── 7) enroll·기동 ─────────────────────────────────────────────────────────────
# ama 데몬은 기동 시 AMX_ENROLL_TOKEN 이 있으면 스스로 enroll 한다(별도 하위명령 없음,
# agent-run.sh 와 동일). systemd --user 가 있으면 서비스로, 없으면 nohup 으로 띄운다.
run_env() {   # 기동에 넘길 env 목록을 표준출력으로 (KEY=VALUE 한 줄씩)
  printf 'AMX_AMS_ADDR=%s\n' "$AMS_ADDR"
  printf 'AMX_AGENT_ID=%s\n' "$AGENT_ID"
  printf 'AMX_STATE_DIR=%s\n' "$STATE_DIR"
  printf 'AMX_AMS_PUBKEY=%s\n' "$PUBKEY"
  printf 'CLAUDE_CONFIG_DIR=%s\n' "$CONFIG_DIR"
  printf 'AMX_TSAMX_BIN=%s\n' "$TSAMX_BIN"
  [ -n "$TOKEN" ] && printf 'AMX_ENROLL_TOKEN=%s\n' "$TOKEN"
  if [ "$INSECURE" = 1 ]; then
    printf 'AMX_GRPC_ALLOW_INSECURE=1\n'
  else
    printf 'AMX_AMS_TLS_CA=%s\n' "$TLS_CA"
  fi
}

started=0
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  step "systemd --user 서비스 등록"
  UNIT_DIR="$HOME/.config/systemd/user"; mkdir -p "$UNIT_DIR"
  {
    printf '[Unit]\nDescription=AMX agent (ama, package install)\nAfter=network-online.target\n\n'
    printf '[Service]\nType=simple\nExecStart=%s\nRestart=on-failure\nRestartSec=5\n' "$BIN"
    # env 는 별도 파일(0600)로 두고 읽는다. enroll 토큰은 1회용이라 소비 후엔
    # 무해하고, 재기동 시 데몬은 저장된 자격증명으로 재접속한다(토큰 미사용).
    printf 'EnvironmentFile=%s\n' "$INSTALL_ROOT/service.env"
    printf '\n[Install]\nWantedBy=default.target\n'
  } > "$UNIT_DIR/amx-agent.service"
  ( umask 077; run_env > "$INSTALL_ROOT/service.env" )
  systemctl --user daemon-reload
  if systemctl --user enable --now amx-agent >/dev/null 2>&1; then
    ok "systemd --user 서비스 기동 (amx-agent)"
    warn "재부팅 후에도 유지하려면: loginctl enable-linger $USER"
    started=1
  else
    warn "systemd --user 기동 실패 — nohup 예비 경로로 전환"
  fi
fi

if [ "$started" != 1 ]; then
  step "nohup 기동"
  launcher="setsid"; command -v setsid >/dev/null 2>&1 || launcher="nohup"
  # env 배열 구성 후 백그라운드 실행.
  ENV_ARGS=()
  while IFS= read -r kv; do [ -n "$kv" ] && ENV_ARGS+=("$kv"); done < <(run_env)
  $launcher env "${ENV_ARGS[@]}" "$BIN" >>"$LOG_DIR/ama.log" 2>&1 &
  echo $! > "$PIDFILE"
  ok "ama 기동 (pid $!) → AMS $AMS_ADDR, 로그 $LOG_DIR/ama.log"
fi

printf '\n'
ok "설치 끝. 관리자 화면(서버 메뉴)에서 이 서버가 '온라인'인지 확인하세요."
printf '%s상태: %s%s\n' "$c_dim" "cat $LOG_DIR/ama.log  (또는 systemctl --user status amx-agent)" "$c_rst"
