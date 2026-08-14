#!/usr/bin/env bash
# agent-install-cmd.sh — PC(중앙 서버)에서 실행. 노트북에 붙여넣을 "에이전트 설치 명령"을
# 완성된 형태로 출력해 준다.
#
# 알아서 해 주는 것: 테넌트 확인 → 서버 행 생성(없으면) → 등록 토큰 발급 →
# LAN IP·서명 공개키 수집 → (WSL이면) portproxy 점검 → 완성 명령 출력.
#
# 사용법:
#   deploy/agent-install-cmd.sh [--server-name 이름] [--tenant 이름] [--tls]
#
#   --server-name  서버 행 이름 (기본: "노트북"). 같은 이름이 있으면 재사용.
#   --tenant       테넌트 이름으로 선택 (기본: 첫 번째 테넌트)
#   --tls          TLS 안내 포함(기본은 평문 --insecure 명령을 출력)
#
# 전제: 이 PC에서 fullstack-run.sh 로 스택이 떠 있어야 한다 (REST :8080).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.amx-dev/dev.env"
REST="http://127.0.0.1:${AMX_DEV_REST_PORT:-8080}"
GRPC_PORT="${AMX_DEV_GRPC_PORT:-50051}"

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_dim=$'\033[2m'; c_rst=$'\033[0m'
ok()   { printf '%s✔%s %s\n' "$c_grn" "$c_rst" "$*"; }
warn() { printf '%s!%s %s\n' "$c_yel" "$c_rst" "$*" >&2; }
die()  { printf '%sx%s %s\n' "$c_red" "$c_rst" "$*" >&2; exit 1; }

SERVER_NAME="노트북"; TENANT_NAME=""; TLS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --server-name) SERVER_NAME="$2"; shift 2 ;;
    --tenant)      TENANT_NAME="$2"; shift 2 ;;
    --tls)         TLS=1; shift ;;
    -h|--help)     sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "알 수 없는 플래그: $1" ;;
  esac
done

command -v python3 >/dev/null 2>&1 || die "python3가 필요합니다"
[ -f "$ENV_FILE" ] || die "dev.env가 없습니다 — 먼저 'deploy/fullstack-run.sh up all'을 실행하세요"
set -a; . "$ENV_FILE"; set +a
curl -fsS "$REST/healthz" >/dev/null 2>&1 || die "REST(:8080)가 응답하지 않습니다 — 'deploy/fullstack-run.sh status' 확인"

api() { # <method> <path> [json-body]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -fsS -X "$method" -H "Authorization: Bearer $AMX_ADMIN_TOKEN" \
      -H 'Content-Type: application/json' -d "$body" "$REST/api/v1$path"
  else
    curl -fsS -X "$method" -H "Authorization: Bearer $AMX_ADMIN_TOKEN" "$REST/api/v1$path"
  fi
}
jget() { python3 -c "import sys,json;d=json.load(sys.stdin);print(eval(sys.argv[1]))" "$1"; }

# 1) 테넌트
TENANTS_JSON="$(api GET /tenants)"
TENANT_ID="$(printf '%s' "$TENANTS_JSON" | python3 -c "
import sys, json
d = json.load(sys.stdin)
name = '''$TENANT_NAME'''
for t in d['items']:
    if not name or t['name'] == name:
        print(t['id']); break
")"
[ -n "$TENANT_ID" ] || die "테넌트가 없습니다 — 관리자 화면에서 '새 테넌트'로 먼저 만드세요"
ok "테넌트: $TENANT_ID"

# 2) 서버 행 (이름으로 찾고, 없으면 생성)
SERVER_ID="$(api GET "/tenants/$TENANT_ID/servers" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for s in d['items']:
    if s['name'] == '''$SERVER_NAME''':
        print(s['id']); break
")"
if [ -n "$SERVER_ID" ]; then
  ok "서버 행 재사용: $SERVER_NAME ($SERVER_ID)"
else
  SERVER_ID="$(api POST "/tenants/$TENANT_ID/servers" \
    "{\"name\":\"$SERVER_NAME\",\"switchMode\":\"manual\"}" | jget "d['id']")"
  ok "서버 행 생성: $SERVER_NAME ($SERVER_ID)"
fi

# 3) 등록 토큰 발급 (한 번만 표시되는 값 — 이 출력에만 나온다)
TOKEN_JSON="$(api POST "/tenants/$TENANT_ID/servers/$SERVER_ID/enroll-token")"
TOKEN="$(printf '%s' "$TOKEN_JSON" | jget "d['token']")"
EXPIRES="$(printf '%s' "$TOKEN_JSON" | jget "d['expiresAt']")"
ok "등록 토큰 발급 (만료 $EXPIRES)"

# 4) 접속 IP — WSL이면 Windows 실 LAN IP를 찾는다 (에이전트는 portproxy를 거쳐 들어온다)
WSL=0; grep -qi microsoft /proc/version 2>/dev/null && WSL=1
WSL_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ "$WSL" = 1 ]; then
  LAN_IP="$(/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -Command \
    "(Get-NetIPConfiguration | Where-Object {\$_.IPv4DefaultGateway -ne \$null} | Select-Object -First 1).IPv4Address.IPAddress" \
    2>/dev/null | tr -d '\r' | head -1)"
  [ -n "$LAN_IP" ] || { warn "Windows LAN IP 자동 감지 실패 — 'ipconfig'로 확인해 아래 <IP>를 바꿔 쓰세요"; LAN_IP="<Windows-LAN-IP>"; }
  ok "Windows LAN IP: $LAN_IP (WSL 내부 IP: $WSL_IP)"
  # portproxy가 현재 WSL IP를 가리키는지 점검 (WSL IP는 재부팅 시 바뀐다)
  MAPPED="$(/mnt/c/Windows/System32/netsh.exe interface portproxy show v4tov4 2>/dev/null | tr -d '\r' \
            | awk -v p="$GRPC_PORT" '$2==p {print $3}' | head -1)"
  # 패키지 설치는 REST(:8080)에서 install.sh·매니페스트·바이너리를 받으므로 gRPC 포트
  # 뿐 아니라 REST 포트도 노트북에서 닿아야 한다.
  REST_PORT="${AMX_DEV_REST_PORT:-8080}"
  REST_MAPPED="$(/mnt/c/Windows/System32/netsh.exe interface portproxy show v4tov4 2>/dev/null | tr -d '\r' \
            | awk -v p="$REST_PORT" '$2==p {print $3}' | head -1)"
  if [ "$REST_MAPPED" != "$WSL_IP" ]; then
    warn "portproxy $REST_PORT(REST)가 현재 WSL IP를 가리키지 않습니다 (현재: ${REST_MAPPED:-없음}) — 다운로드가 실패합니다."
    printf '    netsh interface portproxy add v4tov4 listenport=%s listenaddress=0.0.0.0 connectport=%s connectaddress=%s\n' \
      "$REST_PORT" "$REST_PORT" "$WSL_IP"
  fi
  if [ "$MAPPED" = "$WSL_IP" ]; then
    ok "portproxy $GRPC_PORT → $WSL_IP 정상"
  else
    warn "portproxy가 현재 WSL IP($WSL_IP)를 가리키지 않습니다 (현재: ${MAPPED:-없음})."
    printf '%s  관리자 PowerShell에서 실행하세요:%s\n' "$c_yel" "$c_rst"
    printf '    netsh interface portproxy delete v4tov4 listenport=%s listenaddress=0.0.0.0\n' "$GRPC_PORT"
    printf '    netsh interface portproxy add v4tov4 listenport=%s listenaddress=0.0.0.0 connectport=%s connectaddress=%s\n' \
      "$GRPC_PORT" "$GRPC_PORT" "$WSL_IP"
  fi
else
  LAN_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
  [ -n "$LAN_IP" ] || LAN_IP="$WSL_IP"
  ok "LAN IP: $LAN_IP"
fi

# 5) 완성 명령 출력
SEC_FLAG="--insecure"; PS_SEC_FLAG="-Insecure"; SCHEME="http"
if [ "$TLS" = 1 ]; then
  SEC_FLAG="--ca ./ca.crt"; PS_SEC_FLAG=""; SCHEME="https"
fi
# 다운로드 베이스 — install.sh/install.ps1·매니페스트·바이너리를 받는 REST 면.
AMS_URL="$SCHEME://$LAN_IP:${AMX_DEV_REST_PORT:-8080}"
echo
echo "──────────────────────────────────────────────────────────────"
echo "노트북에서 아래 한 줄을 그대로 실행하세요 (git·go·python 없어도 됩니다):"
echo
printf '%scurl -fsSL %s/install.sh | bash -s -- \\\n' "$c_grn" "$AMS_URL"
printf '  --ams %s:%s \\\n'      "$LAN_IP" "$GRPC_PORT"
printf '  --ams-url %s \\\n'     "$AMS_URL"
printf '  --token %s \\\n'       "$TOKEN"
printf '  --pubkey %s \\\n'      "$AMX_AMS_PUBKEY"
printf '  %s%s\n'                "$SEC_FLAG" "$c_rst"
echo "──────────────────────────────────────────────────────────────"
echo "Windows(PowerShell)라면:"
printf '%s$s = irm %s/install.ps1; & ([scriptblock]::Create($s)) `\n' "$c_grn" "$AMS_URL"
printf '  -Ams %s:%s -AmsUrl %s -Token %s -Pubkey %s %s%s\n' \
  "$LAN_IP" "$GRPC_PORT" "$AMS_URL" "$TOKEN" "$AMX_AMS_PUBKEY" "$PS_SEC_FLAG" "$c_rst"
echo "──────────────────────────────────────────────────────────────"
if [ "$TLS" = 1 ]; then
  warn "TLS 모드: 먼저 deploy/tls/ 로 인증서를 만들고 ca.crt를 노트북에 복사한 뒤 실행하세요 (가이드 B-3 참고)."
else
  warn "신뢰 LAN 한정 — 평문 HTTP로 스크립트·바이너리를 받고 gRPC도 평문(--insecure)입니다."
  warn "매니페스트가 위 공개키로 서명 검증되고 산출물은 sha256 대조되지만, 토큰은 평문으로 흐릅니다."
  warn "PC도 --insecure-grpc로 떠 있어야 하며, 신뢰 LAN 밖에서는 --ca로 TLS를 쓰세요."
fi
printf '%s토큰은 이 출력에만 표시됩니다. 만료(%s) 전에 사용하세요.%s\n' "$c_dim" "$EXPIRES" "$c_rst"
