#!/usr/bin/env bash
# agent-run.sh — 노트북(서버 역할)에서 ama 에이전트를 띄우는 스크립트.
#
# 저장소를 clone 한 뒤 단독으로 실행할 수 있다(풀스택 스크립트와 독립).
# ama 에이전트는 PC의 gRPC 제어면(:50051)에 접속해 이 노트북을 "서버"로 등록한다.
#
# 사용법:
#   deploy/agent-run.sh <up|down|status|logs> [플래그]
#   `up` 을 플래그 없이 부르면 agent-setup.sh install 이 기록한 .amx-agent/setup.env
#   의 설치 인자(--token 제외)를 자동으로 복원해 재기동한다.
#
# 필수(플래그 또는 환경변수):
#   --ams HOST:PORT        PC의 gRPC 주소            (env AMX_AMS_ADDR)
#   --token TOKEN          웹에서 발급한 enroll 토큰  (env AMX_ENROLL_TOKEN) — 최초 등록에 필요
#   --pubkey B64 | --pubkey-file PATH   AMS 서명 공개키 (env AMX_AMS_PUBKEY / AMX_AMS_PUBKEY_FILE)
#                          → PC에서 `deploy/fullstack-run.sh` 최초 up 후 .amx-dev/dev.env 의
#                            AMX_AMS_PUBKEY 값을 그대로 복사해 오세요.
# 보안(둘 중 하나 필수):
#   --ca PATH              AMS 서버 인증서를 검증할 CA(PEM) → TLS 사용 (env AMX_AMS_TLS_CA)
#   --insecure             TLS 없이 평문 접속 (PC도 --insecure-grpc 여야 함). 첫 시험 전용.
# 선택:
#   --server-id ID         등록 대상 서버 행 ID       (env AMX_SERVER_ID)
#   --agent-id ID          에이전트 식별자            (env AMX_AGENT_ID, 기본 ama_dev)
#   --config-dir PATH      Claude 설정 홈(tsamx·자격증명) (env CLAUDE_CONFIG_DIR)
#   --codex-home PATH      Codex 설정 홈(auth.json) — 주면 codex 프로바이더 활성화
#                          (env AMX_CODEX_HOME). 미지정 시 codex 완전 비활성.
#   --tsamx-bin PATH       tsamx 실행파일 경로        (env AMX_TSAMX_BIN, 기본 PATH의 tsamx)
#   --state-dir PATH       에이전트 상태 저장 위치    (env AMX_STATE_DIR, 기본 <repo>/.amx-agent/state)
#   --client-cert / --client-key PATH   mTLS 클라이언트 인증서(선택)
#
# 주의: 배포 에이전트는 항상 실제 tsamx CLI를 호출한다(내장 fake 없음). 계정 하달까지
#       시험하려면 이 노트북에 tsamx가 설치되어 있어야 한다(docs/TSAMX-GUIDE.md).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV_DIR="$ROOT/.amx-agent"
LOG_DIR="$DEV_DIR/logs"
BIN="$DEV_DIR/ama"
# Git Bash(MSYS/MinGW)에서는 go가 만든 실행파일이 Windows PE라 .exe 확장자가
# 있어야 커널이 실행한다. `go build -o`는 이름을 그대로 쓰므로(자동 .exe 없음)
# 여기서 붙여, build의 -o 대상과 기동 시 실행 경로가 동일하게 맞는다.
case "$(uname -s)" in
  MINGW*|MSYS*) BIN="$BIN.exe" ;;
esac
PIDFILE="$DEV_DIR/ama.pid"

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_dim=$'\033[2m'; c_rst=$'\033[0m'
ok()   { printf '%s✔%s %s\n' "$c_grn" "$c_rst" "$*"; }
warn() { printf '%s!%s %s\n' "$c_yel" "$c_rst" "$*" >&2; }
err()  { printf '%sx%s %s\n' "$c_red" "$c_rst" "$*" >&2; }
die()  { err "$*"; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "필요한 명령이 없습니다: $1"; }

# ── env 기본값 ───────────────────────────────────────────────────────────────
AMS_ADDR="${AMX_AMS_ADDR:-}"
ENROLL_TOKEN="${AMX_ENROLL_TOKEN:-}"
PUBKEY="${AMX_AMS_PUBKEY:-}"
PUBKEY_FILE="${AMX_AMS_PUBKEY_FILE:-}"
TLS_CA="${AMX_AMS_TLS_CA:-}"
INSECURE=0
SERVER_ID="${AMX_SERVER_ID:-}"
AGENT_ID="${AMX_AGENT_ID:-ama_dev}"
CONFIG_DIR="${CLAUDE_CONFIG_DIR:-}"
CODEX_HOME_DIR="${AMX_CODEX_HOME:-}"
TSAMX_BIN="${AMX_TSAMX_BIN:-}"
STATE_DIR="${AMX_STATE_DIR:-$DEV_DIR/state}"
CLIENT_CERT="${AMX_AMS_TLS_CLIENT_CERT:-}"
CLIENT_KEY="${AMX_AMS_TLS_CLIENT_KEY:-}"

ACTION="${1:-}"; shift || true

# up 을 플래그 없이 부르면 agent-setup.sh install 이 남긴 .amx-agent/setup.env 의
# 설치 인자를 그대로 다시 쓴다(재기동용). 플래그를 하나라도 주면 파일은 무시한다.
# --token 은 1회용이라 복원하지 않는다(재접속은 저장된 자격증명을 쓴다).
SETUP_ENV="$DEV_DIR/setup.env"
if [ "$ACTION" = up ] && [ $# -eq 0 ] && [ -f "$SETUP_ENV" ]; then
  AMX_SETUP_ARGS=""; AMX_SETUP_CONFIG_DIR=""; AMX_SETUP_TSAMX_BIN=""
  # shellcheck disable=SC1090
  . "$SETUP_ENV"
  # shellcheck disable=SC2206
  restored=(); skip=0
  for w in $AMX_SETUP_ARGS; do
    [ $skip = 1 ] && { skip=0; continue; }
    [ "$w" = --token ] && { skip=1; continue; }
    restored+=("$w")
  done
  set -- "${restored[@]}"
  [ -n "$AMX_SETUP_CONFIG_DIR" ] && set -- --config-dir "$AMX_SETUP_CONFIG_DIR" "$@"
  [ -n "$AMX_SETUP_TSAMX_BIN" ]  && set -- --tsamx-bin "$AMX_SETUP_TSAMX_BIN" "$@"
  ok "설치 설정 복원: $SETUP_ENV"
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --ams)         AMS_ADDR="$2"; shift 2 ;;
    --token)       ENROLL_TOKEN="$2"; shift 2 ;;
    --pubkey)      PUBKEY="$2"; shift 2 ;;
    --pubkey-file) PUBKEY_FILE="$2"; shift 2 ;;
    --ca)          TLS_CA="$2"; shift 2 ;;
    --insecure)    INSECURE=1; shift ;;
    --server-id)   SERVER_ID="$2"; shift 2 ;;
    --agent-id)    AGENT_ID="$2"; shift 2 ;;
    --config-dir)  CONFIG_DIR="$2"; shift 2 ;;
    --codex-home)  CODEX_HOME_DIR="$2"; shift 2 ;;
    --tsamx-bin)   TSAMX_BIN="$2"; shift 2 ;;
    --state-dir)   STATE_DIR="$2"; shift 2 ;;
    --client-cert) CLIENT_CERT="$2"; shift 2 ;;
    --client-key)  CLIENT_KEY="$2"; shift 2 ;;
    *) die "알 수 없는 인자: $1" ;;
  esac
done

is_running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; }

build() {
  need go
  mkdir -p "$DEV_DIR"
  printf '%s빌드: go build ./cmd/ama…%s\n' "$c_dim" "$c_rst"
  # 커밋 해시를 바이너리에 새겨 Register 의 agent_version 으로 올린다. self_update
  # 가 재빌드할 때도 같은 -X main.commit 을 쓰므로, 콘솔에서 보이는 버전 문자열이
  # 두 경로에서 동일하다. 저장소가 아니면 빈 값 → 버전만 표기.
  local sha; sha="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)"
  ( cd "$ROOT/ama-agent" && go build -ldflags "-X main.commit=$sha" -o "$BIN" ./cmd/ama ) || die "go build 실패"
  ok "빌드 완료 → $BIN"
}

agent_up() {
  is_running && { ok "에이전트 이미 실행 중 (pid $(cat "$PIDFILE"))"; return 0; }
  # ── 필수 인자 검증(연결 시도 전 fail-loud) ──
  [ -n "$AMS_ADDR" ] || die "--ams HOST:PORT (또는 AMX_AMS_ADDR)가 필요합니다. 예: --ams 192.168.0.10:50051"
  if [ -z "$PUBKEY" ] && [ -z "$PUBKEY_FILE" ]; then
    die "AMS 서명 공개키가 필요합니다: --pubkey <B64> 또는 --pubkey-file <PATH>.
    PC에서 fullstack-run.sh 최초 up 후 .amx-dev/dev.env 의 AMX_AMS_PUBKEY 값을 복사하세요."
  fi
  if [ "$INSECURE" = 1 ]; then
    warn "평문(TLS 없음)으로 접속합니다 — PC도 --insecure-grpc 여야 합니다. 첫 시험 전용."
  elif [ -n "$TLS_CA" ]; then
    [ -f "$TLS_CA" ] || die "--ca 파일을 찾을 수 없음: $TLS_CA"
  else
    die "보안이 설정되지 않았습니다: --ca <PEM> 로 TLS를 쓰거나, 첫 시험이면 --insecure 를 주세요."
  fi
  [ -n "$ENROLL_TOKEN" ] || warn "enroll 토큰이 없습니다 — 최초 등록이라면 실패합니다(재접속은 저장된 자격증명 사용). 웹의 'Enroll token'에서 발급하세요."
  [ -n "$CONFIG_DIR" ] || warn "CLAUDE_CONFIG_DIR 미설정 — tsamx 계정 하달/자격증명 재동기화가 비활성화됩니다(등록·상태 시험은 가능)."

  build
  mkdir -p "$STATE_DIR" "$LOG_DIR"

  # ── 실행 env 구성 ──
  local -a e=(
    AMX_AMS_ADDR="$AMS_ADDR"
    AMX_AGENT_ID="$AGENT_ID"
    AMX_STATE_DIR="$STATE_DIR"
    # self_update 가 fast-forward 할 작업 트리. 명령에는 소스가 실려 오지 않고,
    # 에이전트는 오직 이 경로의 upstream 만 당긴다.
    AMX_REPO_DIR="$ROOT"
  )
  [ -n "$ENROLL_TOKEN" ] && e+=( AMX_ENROLL_TOKEN="$ENROLL_TOKEN" )
  [ -n "$SERVER_ID" ]    && e+=( AMX_SERVER_ID="$SERVER_ID" )
  [ -n "$PUBKEY" ]       && e+=( AMX_AMS_PUBKEY="$PUBKEY" )
  [ -n "$PUBKEY_FILE" ]  && e+=( AMX_AMS_PUBKEY_FILE="$PUBKEY_FILE" )
  [ -n "$CONFIG_DIR" ]   && e+=( CLAUDE_CONFIG_DIR="$CONFIG_DIR" )
  # 주면 codex 프로바이더가 켜진다(에이전트 게이트). 미지정 시 전달 안 함 → codex 비활성.
  [ -n "$CODEX_HOME_DIR" ] && e+=( AMX_CODEX_HOME="$CODEX_HOME_DIR" )
  [ -n "$TSAMX_BIN" ]    && e+=( AMX_TSAMX_BIN="$TSAMX_BIN" )
  if [ "$INSECURE" = 1 ]; then
    e+=( AMX_GRPC_ALLOW_INSECURE=1 )
  else
    e+=( AMX_AMS_TLS_CA="$TLS_CA" )
    [ -n "$CLIENT_CERT" ] && e+=( AMX_AMS_TLS_CLIENT_CERT="$CLIENT_CERT" )
    [ -n "$CLIENT_KEY" ]  && e+=( AMX_AMS_TLS_CLIENT_KEY="$CLIENT_KEY" )
  fi

  local log="$LOG_DIR/ama.log"
  local launcher="setsid"; command -v setsid >/dev/null 2>&1 || launcher=""
  $launcher env "${e[@]}" "$BIN" >>"$log" 2>&1 &
  echo $! > "$PIDFILE"
  ok "에이전트 기동 (pid $!) → AMS $AMS_ADDR"
  printf '%s로그: %s (status/logs 로 확인)%s\n' "$c_dim" "$log" "$c_rst"
}

agent_down() {
  [ -f "$PIDFILE" ] || { printf '%s에이전트: pidfile 없음%s\n' "$c_dim" "$c_rst"; return 0; }
  local p; p="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
    kill -TERM "-$p" 2>/dev/null || kill -TERM "$p" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$p" 2>/dev/null || break; sleep 0.3; done
    kill -0 "$p" 2>/dev/null && { kill -KILL "-$p" 2>/dev/null || kill -KILL "$p" 2>/dev/null || true; }
    ok "에이전트 종료 (pid $p)"
  else
    printf '%s에이전트: 실행 중 아님%s\n' "$c_dim" "$c_rst"
  fi
  rm -f "$PIDFILE"
}

agent_status() {
  if is_running; then
    ok "에이전트 실행 중 (pid $(cat "$PIDFILE"))"
    printf '%s최근 로그:%s\n' "$c_dim" "$c_rst"
    tail -n 8 "$LOG_DIR/ama.log" 2>/dev/null || true
  else
    err "에이전트 실행 중 아님"
    [ -f "$LOG_DIR/ama.log" ] && { printf '%s마지막 로그:%s\n' "$c_dim" "$c_rst"; tail -n 12 "$LOG_DIR/ama.log"; }
    return 1
  fi
}

case "$ACTION" in
  up)     agent_up ;;
  down)   agent_down ;;
  status) agent_status ;;
  logs)   [ -f "$LOG_DIR/ama.log" ] || die "로그 없음(아직 기동 안 함?)"; exec tail -n 40 -F "$LOG_DIR/ama.log" ;;
  ""|-h|--help|help) sed -n '2,37p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
  *) die "알 수 없는 명령: $ACTION (up|down|status|logs)" ;;
esac
