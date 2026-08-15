#!/usr/bin/env bash
# agent-setup.sh — 노트북(에이전트) 원클릭 설치·제거 스크립트.
#
# PC에서 `deploy/agent-install-cmd.sh`가 출력해 주는 명령을 그대로 붙여넣는 용도다.
# 하는 일: 사전점검(go·uv) → tsamx 설치 → 에이전트 빌드·기동(agent-run.sh) → 성공 판정.
#
# 사용법:
#   deploy/agent-setup.sh install --ams HOST:PORT --token T --pubkey K (--insecure | --ca ca.crt) [옵션]
#   deploy/agent-setup.sh uninstall [--purge-tsamx] [--purge-config] [--yes]
#   deploy/agent-setup.sh status
#
# install 옵션 (agent-run.sh로 그대로 전달):
#   --config-dir PATH   tsamx가 계정을 넣고 뺄 Claude 설정 홈 (기본 ~/.claude-amx)
#   --agent-id ID       에이전트 식별자 (기본 ama_dev)
#   --tsamx-bin PATH    tsamx 실행파일 경로 (기본: 설치 후 자동 탐지)
#
# uninstall 옵션:
#   (기본)          에이전트 종료 + 상태 디렉터리(.amx-agent) 삭제. tsamx·계정은 남긴다.
#   --purge-tsamx   uv tool로 설치한 tsamx도 제거
#   --purge-config  Claude 설정 홈(계정 자격증명 포함!)도 삭제 — 입력 확인을 요구한다
#   --yes           확인 질문 생략(자동화용)
#
# Windows(Git Bash) 제약:
#   - 계정 하달(deliver) 중 러너 기동을 막는 B1b 락 가드가 편측으로만 동작한다.
#     에이전트는 LockFileEx로 락을 잡지만, 러너 래퍼(amx-claude/amx-codex)는
#     flock(1)로 공유 락을 잡는데 Git Bash에는 flock(1)이 없어 조용히 통과한다.
#     즉 Windows 네이티브에서는 러너가 하달 도중에도 기동될 수 있다. 잔여 노출은
#     B1a(이전 active 복원 + 원자적 쓰기)가 1초 미만 스왑 창으로 제한한다.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV_DIR="$ROOT/.amx-agent"
SETUP_ENV="$DEV_DIR/setup.env"

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_dim=$'\033[2m'; c_rst=$'\033[0m'
ok()   { printf '%s✔%s %s\n' "$c_grn" "$c_rst" "$*"; }
warn() { printf '%s!%s %s\n' "$c_yel" "$c_rst" "$*" >&2; }
err()  { printf '%sx%s %s\n' "$c_red" "$c_rst" "$*" >&2; }
die()  { err "$*"; exit 1; }

ACTION="${1:-}"; shift || true

# ── install ──────────────────────────────────────────────────────────────────
do_install() {
  local config_dir="$HOME/.claude-amx"
  local tsamx_bin="" pass=()          # agent-run.sh로 넘길 나머지 플래그
  while [ $# -gt 0 ]; do
    case "$1" in
      --config-dir) config_dir="$2"; shift 2 ;;
      --tsamx-bin)  tsamx_bin="$2"; shift 2 ;;
      *) pass+=("$1"); shift ;;
    esac
  done

  echo "── 1/4 사전점검 ─────────────────────────────"
  command -v go >/dev/null 2>&1 \
    || die "Go가 없습니다 (1.24+ 필요). 설치: https://go.dev/dl/ 에서 받아 /usr/local/go 에 풀고 PATH에 추가"
  ok "go $(go version | awk '{print $3}')"
  command -v uv >/dev/null 2>&1 \
    || die "uv가 없습니다. 설치:  curl -LsSf https://astral.sh/uv/install.sh | sh  실행 후 셸 재시작"
  ok "uv $(uv --version | awk '{print $2}')"

  echo "── 2/4 tsamx 설치 ───────────────────────────"
  if [ -z "$tsamx_bin" ]; then
    if command -v tsamx >/dev/null 2>&1; then
      ok "tsamx 이미 설치됨 ($(command -v tsamx))"
    else
      uv tool install --editable "$ROOT/tsamx" >/dev/null \
        || die "tsamx 설치 실패 — 'uv tool install --editable $ROOT/tsamx' 출력을 확인하세요"
      # uv tool 설치 경로가 아직 PATH에 없을 수 있어 절대 경로로 고정한다.
      command -v tsamx >/dev/null 2>&1 || export PATH="$HOME/.local/bin:$PATH"
      command -v tsamx >/dev/null 2>&1 || die "tsamx가 PATH에 없습니다 — ~/.local/bin을 PATH에 추가하세요"
      ok "tsamx 설치 완료 ($(command -v tsamx))"
    fi
    tsamx_bin="$(command -v tsamx)"
  else
    [ -x "$tsamx_bin" ] || die "--tsamx-bin 경로가 실행파일이 아닙니다: $tsamx_bin"
    ok "tsamx 지정 경로 사용 ($tsamx_bin)"
  fi

  echo "── 3/4 에이전트 기동 ────────────────────────"
  mkdir -p "$config_dir"
  bash "$ROOT/deploy/agent-run.sh" up \
    --config-dir "$config_dir" --tsamx-bin "$tsamx_bin" "${pass[@]}"

  # Langfuse 추적 opt-in: AMX_LANGFUSE_* 세 변수가 모두 있을 때만 훅을 설치한다.
  # 하나라도 비면 완전 무동작(기존 설치 경로 불변). 실패는 경고만 — 에이전트
  # 설치 자체를 막지 않는다. 같은 설정 홈(config_dir)에 심는다.
  if [ -n "${AMX_LANGFUSE_BASE_URL:-}" ] \
     && [ -n "${AMX_LANGFUSE_PUBLIC_KEY:-}" ] \
     && [ -n "${AMX_LANGFUSE_SECRET_KEY:-}" ]; then
    echo "   Langfuse 추적 설치 (AMX_LANGFUSE_* 감지)…"
    if LANGFUSE_BASE_URL="$AMX_LANGFUSE_BASE_URL" \
       LANGFUSE_PUBLIC_KEY="$AMX_LANGFUSE_PUBLIC_KEY" \
       LANGFUSE_SECRET_KEY="$AMX_LANGFUSE_SECRET_KEY" \
       sh "$ROOT/deploy/install-langfuse-hook.sh" --config-dir "$config_dir"; then
      ok "Langfuse 훅 설치 완료 ($config_dir)"
    else
      warn "Langfuse 훅 설치 실패 — 추적 없이 계속합니다. 나중에 deploy/install-langfuse-hook.sh로 재시도하세요."
    fi
  fi

  # 겸용 PC 프로필 부트스트랩: 개인 프로필(~/.claude)이 있고 유저 스코프 자산
  # (CLAUDE.md·skills·projects) 중 하나라도 있으면, 그 지침·스킬·메모리를 러너
  # 프로필($config_dir)에서 링크로 공유하도록 연결한다. 링크는 러너 홈 안에만
  # 만들며 개인 프로필은 읽기만 한다(deploy/bootstrap-profile.sh 참고).
  # 개인 프로필이 없거나 자산이 없으면 완전 무동작. 실패는 경고만 —
  # 에이전트 설치를 막지 않는다.
  if [ -d "$HOME/.claude" ] \
     && { [ -e "$HOME/.claude/CLAUDE.md" ] || [ -d "$HOME/.claude/skills" ] || [ -d "$HOME/.claude/projects" ]; }; then
    echo "   겸용 PC 프로필 부트스트랩 (개인 프로필 감지)…"
    if sh "$ROOT/deploy/bootstrap-profile.sh" --personal-dir "$HOME/.claude" --config-dir "$config_dir"; then
      ok "프로필 부트스트랩 완료 (개인→러너 자산 링크)"
    else
      warn "프로필 부트스트랩 실패 — 자산 공유 없이 계속합니다. 나중에 deploy/bootstrap-profile.sh로 재시도하세요."
    fi
  fi

  # 재기동·제거를 위해 (토큰 제외) 설정을 기록해 둔다. 토큰은 1회용이라 저장하지 않는다.
  mkdir -p "$DEV_DIR"
  {
    printf '# agent-setup.sh가 기록한 설치 설정 (토큰 제외). 재기동 시 참고용.\n'
    printf 'AMX_SETUP_CONFIG_DIR=%q\n' "$config_dir"
    printf 'AMX_SETUP_TSAMX_BIN=%q\n' "$tsamx_bin"
    printf 'AMX_SETUP_ARGS=%q\n' "${pass[*]}"
  } > "$SETUP_ENV"

  # tsclaude 래퍼: 이 설치의 설정 홈(config_dir)과 tsamx 경로를 각인해, 운영자가
  # `tsclaude list` 만으로 `CLAUDE_CONFIG_DIR=<홈> tsamx list` 와 동일하게 이 서버의
  # AMX 풀을 조회하도록 한다. 이미 있으면 덮어쓴다.
  local wrapper_dir="$HOME/.local/bin"
  local wrapper="$wrapper_dir/tsclaude"
  if [ -n "$config_dir" ]; then
    mkdir -p "$wrapper_dir"
    {
      printf '#!/usr/bin/env bash\n'
      printf '# AMX tsclaude 래퍼 — deploy/agent-setup.sh install 시점에 각인됨.\n'
      printf '# 이 서버의 Claude 설정 홈을 주입해 tsamx를 호출한다.\n'
      printf '# 재생성: install 재실행 / 제거: uninstall.\n'
      printf 'exec env CLAUDE_CONFIG_DIR=%q %q "$@"\n' "$config_dir" "$tsamx_bin"
    } > "$wrapper"
    chmod +x "$wrapper"
    ok "tsclaude 래퍼 생성 ($wrapper → CLAUDE_CONFIG_DIR=$config_dir)"
    case ":$PATH:" in
      *":$wrapper_dir:"*) ;;
      *) warn "$wrapper_dir 가 PATH에 없습니다 — tsclaude를 쓰려면 PATH에 추가하세요." ;;
    esac
  else
    warn "config-dir가 비어 tsclaude 래퍼 생성을 건너뜁니다."
  fi

  echo "── 4/4 성공 판정 ────────────────────────────"
  sleep 2
  bash "$ROOT/deploy/agent-run.sh" status || true
  if tail -n 20 "$DEV_DIR/logs/ama.log" 2>/dev/null | grep -qiE 'error|refused|denied|invalid'; then
    warn "최근 로그에 오류가 보입니다 — 'deploy/agent-run.sh logs' 로 확인하세요."
  else
    ok "로그에 연결 오류 없음"
  fi
  echo
  ok "설치 끝. 최종 확인은 관리자 화면(서버 메뉴)에서 이 서버가 '온라인'인지 보세요."
  printf '%s제거는: deploy/agent-setup.sh uninstall  (계정까지 지우려면 --purge-config)%s\n' "$c_dim" "$c_rst"
}

# ── uninstall ────────────────────────────────────────────────────────────────
do_uninstall() {
  local purge_tsamx=0 purge_config=0 yes=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --purge-tsamx)  purge_tsamx=1; shift ;;
      --purge-config) purge_config=1; shift ;;
      --yes)          yes=1; shift ;;
      *) die "알 수 없는 플래그: $1" ;;
    esac
  done

  local config_dir=""
  [ -f "$SETUP_ENV" ] && config_dir="$(sed -n 's/^AMX_SETUP_CONFIG_DIR=//p' "$SETUP_ENV" | tr -d "'")"

  echo "제거 대상:"
  echo "  - 에이전트 프로세스 + 상태 디렉터리 ($DEV_DIR)"
  [ "$purge_tsamx" = 1 ]  && echo "  - tsamx (uv tool)"
  [ "$purge_config" = 1 ] && echo "  - Claude 설정 홈 ${config_dir:-~/.claude-amx} (계정 자격증명 포함!)"
  if [ "$yes" != 1 ]; then
    printf '계속하려면 yes 입력: '
    read -r answer
    [ "$answer" = "yes" ] || die "중단했습니다 (아무것도 지우지 않음)"
  fi

  bash "$ROOT/deploy/agent-run.sh" down || true
  rm -rf "$DEV_DIR"
  ok "에이전트 종료·상태 삭제 완료"

  # install이 각인한 tsclaude 래퍼만 제거. 동명의 사용자 스크립트를 지우지 않도록
  # 우리 마커("# AMX tsclaude 래퍼")가 있을 때만 삭제한다.
  local wrapper="$HOME/.local/bin/tsclaude"
  if [ -f "$wrapper" ]; then
    if grep -q '^# AMX tsclaude 래퍼' "$wrapper" 2>/dev/null; then
      rm -f "$wrapper" && ok "tsclaude 래퍼 제거 완료 ($wrapper)"
    else
      warn "$wrapper 는 AMX 래퍼가 아니어서 건드리지 않았습니다."
    fi
  fi

  if [ "$purge_tsamx" = 1 ]; then
    if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q '^tsamx'; then
      uv tool uninstall tsamx >/dev/null && ok "tsamx 제거 완료"
    else
      warn "uv tool 목록에 tsamx가 없어 건너뜀"
    fi
  fi

  if [ "$purge_config" = 1 ]; then
    local target="${config_dir:-$HOME/.claude-amx}"
    if [ -d "$target" ]; then
      rm -rf "$target" && ok "설정 홈 삭제 완료 ($target)"
    else
      warn "설정 홈이 없어 건너뜀 ($target)"
    fi
  else
    [ -n "$config_dir" ] && printf '%s설정 홈은 남겨두었습니다: %s (계정 자격증명 보존)%s\n' "$c_dim" "$config_dir" "$c_rst"
  fi
  ok "제거 끝"
}

case "$ACTION" in
  install)   do_install "$@" ;;
  uninstall) do_uninstall "$@" ;;
  status)    exec bash "$ROOT/deploy/agent-run.sh" status ;;
  ""|-h|--help|help) sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
  *) die "알 수 없는 명령: $ACTION (install|uninstall|status)" ;;
esac
