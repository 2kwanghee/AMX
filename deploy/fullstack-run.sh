#!/usr/bin/env bash
# fullstack-run.sh — 개발·테스트용 AMX 풀스택 기동기 (PC용).
#
# 사용법:
#   deploy/fullstack-run.sh <up|down|restart|status|logs> [db|server|web|all] [플래그]
#   deploy/fullstack-run.sh bootstrap-admin <email> <password> [role]
#
#   구성요소:
#     db      개발용 PostgreSQL (도커 컨테이너)
#     server  REST(:8080) + gRPC 제어면(:50051) 한 쌍
#     web     관리자 화면(:3000, 운영 빌드로만 — next dev 금지)
#     all     위 전부 (기본값)
#
#   플래그:
#     --lan             REST·web을 0.0.0.0에 바인딩하고 감지한 LAN IP를 출력.
#                       (gRPC는 소스가 항상 모든 인터페이스에 바인딩한다 — 아래 참고)
#     --insecure-grpc   gRPC를 평문으로 (AMX_GRPC_ALLOW_INSECURE=1). 첫 시험 전용, 경고 출력.
#                       미지정 시: dev.env에 TLS 인증서가 있으면 TLS 사용, 없으면 기동 거부(fail-loud).
#
# 최초 up 시 시크릿을 자동 생성해 저장소 루트 .amx-dev/dev.env(0600)에 보관한다.
# pidfile은 .amx-dev/*.pid, 로그는 .amx-dev/logs/. 종료는 pidfile/포트 기반만 사용한다(pkill -f 금지).
set -euo pipefail

# ── 경로·상수 ────────────────────────────────────────────────────────────────
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV_DIR="$ROOT/.amx-dev"
ENV_FILE="$DEV_DIR/dev.env"
LOG_DIR="$DEV_DIR/logs"

DB_PORT="${AMX_DEV_DB_PORT:-55432}"
REST_PORT="${AMX_DEV_REST_PORT:-8080}"
GRPC_PORT="${AMX_DEV_GRPC_PORT:-50051}"
WEB_PORT="${AMX_DEV_WEB_PORT:-3000}"
DB_CONTAINER="amx-dev-pg"
DB_IMAGE="postgres:16-alpine"
DB_NAME="amx"
DB_USER="amx"

LAN=0
INSECURE_GRPC=0

# ── 출력 헬퍼 ────────────────────────────────────────────────────────────────
c_red=$'\033[31m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_dim=$'\033[2m'; c_rst=$'\033[0m'
info()  { printf '%s\n' "$*"; }
ok()    { printf '%s✔%s %s\n' "$c_grn" "$c_rst" "$*"; }
warn()  { printf '%s!%s %s\n' "$c_yel" "$c_rst" "$*" >&2; }
err()   { printf '%sx%s %s\n' "$c_red" "$c_rst" "$*" >&2; }
die()   { err "$*"; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "필요한 명령이 없습니다: $1"; }

# ── LAN IP 감지 ──────────────────────────────────────────────────────────────
lan_ip() {
  # WSL2에서 `ip route`의 src는 172.x 내부 IP라 에이전트가 도달할 수 없다.
  # WSL이면 Windows 실 LAN IP를 얻는다(로직은 deploy/agent-install-cmd.sh와 동일).
  if grep -qi microsoft /proc/version 2>/dev/null; then
    /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -Command \
      "(Get-NetIPConfiguration | Where-Object {\$_.IPv4DefaultGateway -ne \$null} | Select-Object -First 1).IPv4Address.IPAddress" \
      2>/dev/null | tr -d '\r' | head -1 || true
    return
  fi
  local ip=""
  ip="$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
  [ -n "$ip" ] || ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  printf '%s' "$ip"
}

# ── 시크릿 생성/로드 ─────────────────────────────────────────────────────────
gen_env() {
  mkdir -p "$DEV_DIR" "$LOG_DIR"
  info "${c_dim}시크릿을 처음 생성합니다 → $ENV_FILE${c_rst}"
  local secrets
  secrets="$(cd "$ROOT/ams-server" && uv run python - <<'PY'
import base64, secrets
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization as s
sk = Ed25519PrivateKey.generate()
seed = sk.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())
pub = sk.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)
print("AMX_ENCRYPTION_KEY=" + Fernet.generate_key().decode())
print("AMX_ADMIN_TOKEN=" + secrets.token_hex(24))
print("AMX_SESSION_SECRET=" + secrets.token_hex(24))
print("AMX_SIGNING_KEY=" + base64.urlsafe_b64encode(seed).decode().rstrip("="))
print("AMX_AMS_PUBKEY=" + base64.b64encode(pub).decode())
print("AMX_DB_PASSWORD=" + secrets.token_hex(12))
PY
)" || die "시크릿 생성 실패 (ams-server uv 환경 확인)"
  local dbpass; dbpass="$(printf '%s\n' "$secrets" | sed -n 's/^AMX_DB_PASSWORD=//p')"
  {
    printf '# AMX 개발 시크릿 — 자동 생성. 커밋 금지(.gitignore로 제외됨).\n'
    printf '%s\n' "$secrets"
    printf 'AMX_DATABASE_URL=postgresql+psycopg://%s:%s@127.0.0.1:%s/%s\n' \
      "$DB_USER" "$dbpass" "$DB_PORT" "$DB_NAME"
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  ok "시크릿 생성 완료 (0600)"
}

load_env() {
  [ -f "$ENV_FILE" ] || gen_env
  set -a; . "$ENV_FILE"; set +a
}

# ── pidfile 기반 프로세스 관리 (pkill -f 절대 금지) ──────────────────────────
pidf() { printf '%s/%s.pid' "$DEV_DIR" "$1"; }

is_running() {
  local f; f="$(pidf "$1")"
  [ -f "$f" ] || return 1
  local p; p="$(cat "$f" 2>/dev/null)"
  [ -n "$p" ] && kill -0 "$p" 2>/dev/null
}

# start_bg <name> <workdir> <command...>  — 새 프로세스 그룹으로 백그라운드 기동
start_bg() {
  local name="$1" wd="$2"; shift 2
  local log; log="$LOG_DIR/$name.log"
  mkdir -p "$LOG_DIR"
  if is_running "$name"; then ok "$name 이미 실행 중 (pid $(cat "$(pidf "$name")"))"; return 0; fi
  local launcher="setsid"
  command -v setsid >/dev/null 2>&1 || launcher=""
  # 서브셸에서 workdir로 이동 후 exec — 새 세션(pgid=pid)이라 종료 시 그룹 전체를 정리.
  $launcher bash -c "cd '$wd' && exec \"\$@\"" _ "$@" >>"$log" 2>&1 &
  echo $! > "$(pidf "$name")"
  ok "$name 기동 (pid $!) — 로그: $log"
}

stop_one() {
  local name="$1" f; f="$(pidf "$name")"
  [ -f "$f" ] || { info "${c_dim}$name: pidfile 없음, 건너뜀${c_rst}"; return 0; }
  local p; p="$(cat "$f" 2>/dev/null || true)"
  if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
    # 프로세스 그룹 전체에 TERM (setsid로 pgid=pid). 실패 시 단일 pid로.
    kill -TERM "-$p" 2>/dev/null || kill -TERM "$p" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$p" 2>/dev/null || break; sleep 0.3; done
    kill -0 "$p" 2>/dev/null && { kill -KILL "-$p" 2>/dev/null || kill -KILL "$p" 2>/dev/null || true; }
    ok "$name 종료 (pid $p)"
  else
    info "${c_dim}$name: 실행 중 아님${c_rst}"
  fi
  rm -f "$f"
}

port_listening() { ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$1\$"; }

# ── DB ───────────────────────────────────────────────────────────────────────
db_up() {
  need docker
  if docker ps --format '{{.Names}}' | grep -qx "$DB_CONTAINER"; then
    ok "db 이미 실행 중 ($DB_CONTAINER)"
  elif docker ps -a --format '{{.Names}}' | grep -qx "$DB_CONTAINER"; then
    docker start "$DB_CONTAINER" >/dev/null && ok "db 재시작 ($DB_CONTAINER)"
  else
    docker run -d --name "$DB_CONTAINER" \
      -e POSTGRES_USER="$DB_USER" -e POSTGRES_PASSWORD="$AMX_DB_PASSWORD" \
      -e POSTGRES_DB="$DB_NAME" -p "$DB_PORT:5432" "$DB_IMAGE" >/dev/null \
      && ok "db 컨테이너 생성 ($DB_CONTAINER, host:$DB_PORT)"
  fi
  info "${c_dim}db 준비 대기…${c_rst}"
  for _ in $(seq 1 30); do
    docker exec "$DB_CONTAINER" pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1 && { ok "db 준비됨"; return 0; }
    sleep 1
  done
  die "db가 제 시간에 준비되지 않음 — docker logs $DB_CONTAINER 확인"
}
db_down() {
  need docker
  if docker ps -a --format '{{.Names}}' | grep -qx "$DB_CONTAINER"; then
    docker rm -f "$DB_CONTAINER" >/dev/null && ok "db 컨테이너 제거"
  else
    info "${c_dim}db: 컨테이너 없음${c_rst}"
  fi
}

# ── server (REST + gRPC) ─────────────────────────────────────────────────────
migrate() {
  info "${c_dim}alembic upgrade head…${c_rst}"
  ( cd "$ROOT/ams-server" && uv run alembic upgrade head ) || die "DB 마이그레이션 실패"
  ok "DB 스키마 최신"
}

# gRPC의 TLS/평문 결정 — 결과를 전역 GRPC_ENV 배열에 채운다.
resolve_grpc_security() {
  GRPC_MODE=""
  if [ "$INSECURE_GRPC" = 1 ]; then
    GRPC_MODE="insecure"
    warn "gRPC를 평문으로 기동합니다 (AMX_GRPC_ALLOW_INSECURE=1) — KEK가 네트워크에 노출됩니다. 첫 시험 전용, 운영 금지."
  elif [ -n "${AMX_GRPC_TLS_CERT:-}" ] && [ -n "${AMX_GRPC_TLS_KEY:-}" ]; then
    GRPC_MODE="tls"
  else
    die "gRPC 보안이 설정되지 않았습니다. 다음 중 하나를 하세요:
    - 첫 시험이면 --insecure-grpc 로 평문 허용, 또는
    - dev.env에 AMX_GRPC_TLS_CERT / AMX_GRPC_TLS_KEY (그리고 mTLS면 AMX_GRPC_TLS_CA) 지정.
    TLS 인증서는 deploy/tls/make-ca.sh · issue-cert.sh 로 발급할 수 있습니다."
  fi
}

server_up() {
  need uv
  db_up
  migrate
  local rest_bind="127.0.0.1"
  [ "$LAN" = 1 ] && rest_bind="0.0.0.0"
  resolve_grpc_security

  # 공통 서버 env(dev.env는 이미 load_env로 export됨). REST 기동.
  # 콘솔 설치 명령을 위해 REST도 서명키(공개키 파생)·gRPC 포트를 받고, --lan이면
  # 그 회차에 감지한 LAN IP를 광고 host로 이 프로세스 env에만 주입한다(영구화 없음 —
  # LAN IP는 회차마다 바뀔 수 있다). --lan 없이는 광고하지 않으며, 운영자가 환경변수로
  # AMX_ADVERTISE_HOST를 직접 지정한 경우는 dev.env 상속으로 그대로 존중된다.
  local rest_env=( AMX_DATABASE_URL="$AMX_DATABASE_URL" AMX_ENCRYPTION_KEY="$AMX_ENCRYPTION_KEY"
                   AMX_ADMIN_TOKEN="$AMX_ADMIN_TOKEN" AMX_SIGNING_KEY="$AMX_SIGNING_KEY"
                   AMX_GRPC_PORT="$GRPC_PORT" )
  # 산출물 배포(/download, /install.sh). dev에서는 build-artifacts.sh가 채우는
  # <repo>/dist를 그대로 서빙하고, 설치 스크립트는 repo의 deploy/에서 읽는다.
  # dist가 아직 없으면 주입하지 않는다 — 배포 비활성으로 두는 편이 빈 디렉터리를
  # 가리켜 404를 흩뿌리는 것보다 낫다.
  if [ -d "$ROOT/dist" ]; then
    rest_env+=( AMX_ARTIFACTS_DIR="$ROOT/dist" AMX_INSTALL_SCRIPTS_DIR="$ROOT/deploy" )
  fi
  if [ "$LAN" = 1 ]; then
    local adv_host; adv_host="$(lan_ip)"
    [ -n "$adv_host" ] && rest_env+=( AMX_ADVERTISE_HOST="$adv_host" )
  fi
  start_bg ams-rest "$ROOT/ams-server" \
    env "${rest_env[@]}" \
        uv run python -m uvicorn app.main:create_app --factory --host "$rest_bind" --port "$REST_PORT"

  # gRPC 제어면(별도 프로세스). 항상 [::](모든 인터페이스)에 바인딩된다.
  local grpc_env=( AMX_DATABASE_URL="$AMX_DATABASE_URL" AMX_ENCRYPTION_KEY="$AMX_ENCRYPTION_KEY"
                   AMX_ADMIN_TOKEN="$AMX_ADMIN_TOKEN" AMX_SIGNING_KEY="$AMX_SIGNING_KEY"
                   AMX_GRPC_PORT="$GRPC_PORT" )
  if [ "$GRPC_MODE" = insecure ]; then
    grpc_env+=( AMX_GRPC_ALLOW_INSECURE=1 )
  else
    grpc_env+=( AMX_GRPC_TLS_CERT="$AMX_GRPC_TLS_CERT" AMX_GRPC_TLS_KEY="$AMX_GRPC_TLS_KEY" )
    [ -n "${AMX_GRPC_TLS_CA:-}" ] && grpc_env+=( AMX_GRPC_TLS_CA="$AMX_GRPC_TLS_CA" )
  fi
  start_bg ams-grpc "$ROOT/ams-server" env "${grpc_env[@]}" uv run python -m app.grpc.server
}

server_down() { stop_one ams-rest; stop_one ams-grpc; }

# ── web ──────────────────────────────────────────────────────────────────────
web_needs_build() {
  [ -f "$ROOT/ams-web/.next/BUILD_ID" ] || return 0
  [ -n "$(find "$ROOT/ams-web/src" "$ROOT/ams-web/package.json" "$ROOT/ams-web/next.config.mjs" \
      -newer "$ROOT/ams-web/.next/BUILD_ID" 2>/dev/null | head -1)" ]
}

web_up() {
  need node; need npm
  local web_bind="127.0.0.1"
  [ "$LAN" = 1 ] && web_bind="0.0.0.0"
  local api_base="http://127.0.0.1:$REST_PORT/api/v1"
  local -a web_env=(
    AMX_API_BASE="$api_base"
    AMX_ADMIN_TOKEN="$AMX_ADMIN_TOKEN"
    AMX_SESSION_SECRET="$AMX_SESSION_SECRET"
  )
  [ -d "$ROOT/ams-web/node_modules" ] || ( cd "$ROOT/ams-web" && info "${c_dim}npm ci…${c_rst}" && npm ci )
  if web_needs_build; then
    info "${c_dim}next build (dev 모드 불가 — CSP가 unsafe-eval을 막음)…${c_rst}"
    ( cd "$ROOT/ams-web" && env "${web_env[@]}" npm run build ) || die "web 빌드 실패"
    ok "web 빌드 완료"
  else
    ok "web 빌드 최신 (.next 재사용)"
  fi
  start_bg ams-web "$ROOT/ams-web" \
    env "${web_env[@]}" npm run start -- -H "$web_bind" -p "$WEB_PORT"
}

web_down() { stop_one ams-web; }

# ── status ───────────────────────────────────────────────────────────────────
check() { # <label> <cmd...>
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$label"; else err "$label"; fi
}
status_all() {
  info "AMX 개발 스택 상태:"
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$DB_CONTAINER"; then
    check "db        pg_isready (:$DB_PORT)" docker exec "$DB_CONTAINER" pg_isready -U "$DB_USER" -d "$DB_NAME"
  else
    err "db        컨테이너 없음"
  fi
  check "server    REST /healthz (:$REST_PORT)" curl -fsS "http://127.0.0.1:$REST_PORT/healthz"
  if port_listening "$GRPC_PORT"; then ok "server    gRPC 리슨 (:$GRPC_PORT)"; else err "server    gRPC 리슨 안 함 (:$GRPC_PORT)"; fi
  local code; code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$WEB_PORT/login" 2>/dev/null || true)"
  if [ "$code" = 200 ]; then ok "web       /login 200 (:$WEB_PORT)"; else err "web       /login=$code (:$WEB_PORT)"; fi
  if [ "$LAN" = 1 ]; then
    local ip; ip="$(lan_ip)"
    info ""
    info "LAN 접속 주소 (노트북 에이전트용):"
    info "  gRPC : ${ip:-<IP감지실패>}:$GRPC_PORT   → 노트북 AMX_AMS_ADDR"
    info "  web  : http://${ip:-<IP>}:$WEB_PORT"
    info "  방화벽에서 위 포트를 여세요 (Windows: 인바운드 규칙 / ufw: allow)."
  fi
}

# ── logs ─────────────────────────────────────────────────────────────────────
logs_for() {
  local comp="${1:-all}"; local -a names
  case "$comp" in
    db)     need docker; exec docker logs -f "$DB_CONTAINER" ;;
    server) names=(ams-rest ams-grpc) ;;
    web)    names=(ams-web) ;;
    all|"") names=(ams-rest ams-grpc ams-web) ;;
    *) die "logs 대상: db|server|web|all" ;;
  esac
  local -a files=(); local n
  for n in "${names[@]}"; do [ -f "$LOG_DIR/$n.log" ] && files+=("$LOG_DIR/$n.log"); done
  [ "${#files[@]}" -gt 0 ] || die "로그 파일이 없습니다 (아직 기동 안 함?)"
  exec tail -n 40 -F "${files[@]}"
}

# ── bootstrap-admin ──────────────────────────────────────────────────────────
bootstrap_admin() {
  local email="${1:-}" password="${2:-}" role="${3:-global-admin}"
  [ -n "$email" ] && [ -n "$password" ] || die "사용법: fullstack-run.sh bootstrap-admin <email> <password> [role]"
  need curl
  info "관리자 생성: $email ($role)"
  local body; body="$(printf '{"email":"%s","password":"%s","role":"%s"}' "$email" "$password" "$role")"
  local out code
  out="$(curl -s -w '\n%{http_code}' -X POST "http://127.0.0.1:$REST_PORT/api/v1/admins" \
        -H "Authorization: Bearer $AMX_ADMIN_TOKEN" -H 'Content-Type: application/json' \
        -d "$body")"
  code="$(printf '%s' "$out" | tail -n1)"
  body="$(printf '%s' "$out" | sed '$d')"
  case "$code" in
    200|201) ok "관리자 생성 완료 — 이 email/password로 웹 로그인" ;;
    422) err "거부됨(422): email 도메인 검증 실패 가능. 예약/특수 도메인(amx.local 등)은 거부됩니다 — example.com 같은 일반 도메인을 쓰세요."; info "$body"; exit 1 ;;
    409) warn "이미 존재하는 관리자(409)"; info "$body" ;;
    401|403) die "인증 실패($code): AMX_ADMIN_TOKEN 불일치 또는 서버 미기동" ;;
    *) err "실패($code)"; info "$body"; exit 1 ;;
  esac
}

# ── 인자 파싱 ────────────────────────────────────────────────────────────────
ACTION="${1:-}"; shift || true
COMP="all"; POS=()
for a in "$@"; do
  case "$a" in
    --lan) LAN=1 ;;
    --insecure-grpc) INSECURE_GRPC=1 ;;
    db|server|web|all) COMP="$a" ;;
    *) POS+=("$a") ;;
  esac
done

do_up()   { case "$1" in db) db_up;; server) server_up;; web) web_up;; all) server_up; web_up;; esac; }

# up/restart 직후 예열이 끝나기 전에 status를 찍으면 x로 보여 오해를 부른다.
# 대상 구성요소가 전부 응답할 때까지 최대 60초 조용히 기다린 뒤 상태를 출력한다.
comp_ready() { # <comp> — 준비되면 0
  case "$1" in
    db)     docker exec "$DB_CONTAINER" pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1 ;;
    server) curl -fsS "http://127.0.0.1:$REST_PORT/healthz" >/dev/null 2>&1 && port_listening "$GRPC_PORT" ;;
    web)    [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$WEB_PORT/login" 2>/dev/null)" = 200 ] ;;
    all)    comp_ready server && comp_ready web ;;
  esac
}
wait_ready() { # <comp>
  local i
  info "예열 대기 중… (최대 60초)"
  for i in $(seq 1 30); do
    comp_ready "$1" && return 0
    sleep 2
  done
  warn "60초 안에 전부 준비되지 않았습니다 — 아래 상태에서 x인 항목은 'logs' 명령으로 원인을 확인하세요."
  return 0
}
do_down() { case "$1" in db) db_down;; server) server_down;; web) web_down;; all) web_down; server_down; db_down;; esac; }

case "$ACTION" in
  up)
    load_env; do_up "$COMP"
    wait_ready "$COMP"
    info ""; status_all
    ;;
  down)
    [ -f "$ENV_FILE" ] && load_env || true
    do_down "$COMP"
    ;;
  restart)
    [ -f "$ENV_FILE" ] && load_env || gen_env
    do_down "$COMP"; sleep 1; load_env; do_up "$COMP"
    wait_ready "$COMP"
    info ""; status_all
    ;;
  status)
    [ -f "$ENV_FILE" ] && load_env || true
    status_all
    ;;
  logs)
    logs_for "$COMP"
    ;;
  bootstrap-admin)
    load_env; bootstrap_admin "${POS[@]:-}"
    ;;
  ""|-h|--help|help)
    sed -n '2,33p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    ;;
  *)
    die "알 수 없는 명령: $ACTION (up|down|restart|status|logs|bootstrap-admin)"
    ;;
esac
