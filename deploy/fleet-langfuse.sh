#!/bin/sh
# fleet-langfuse.sh — Langfuse 추적 훅을 여러 러너 호스트에 일괄 on/off/status.
#
# 개별 호스트에서 `deploy/install-langfuse-hook.sh`를 손으로 돌리는 대신, 호스트
# 목록을 받아 ssh로 한 번에 켜고(끄고) 상태를 조회한다. 각 호스트의 실제 설치·
# 제거 로직은 그 호스트에 체크아웃된 install-langfuse-hook.sh가 수행하고, 이
# 스크립트는 오케스트레이션만 한다(원격 스크립트 내부는 건드리지 않는다).
#
# 사용법
# ------
#   # 켜기: 세 자격증명을 환경에 넣고 실행(argv/로그에 시크릿을 남기지 않음)
#   LANGFUSE_BASE_URL=http://langfuse:3100 \
#   LANGFUSE_PUBLIC_KEY=pk-... \
#   LANGFUSE_SECRET_KEY=sk-... \
#     sh deploy/fleet-langfuse.sh on
#
#   sh deploy/fleet-langfuse.sh off              # 전 호스트에서 추적 회수
#   sh deploy/fleet-langfuse.sh status           # 호스트별 env 파일 존재 여부
#
# 옵션
# ----
#   --hosts FILE       호스트 목록 파일 (기본 deploy/fleet-hosts.txt)
#                      한 줄에 하나씩 `user@host`. 빈 줄과 `#` 주석은 무시.
#   --remote-repo PATH 원격 호스트의 AMX 체크아웃 경로 (기본 $HOME/AMX).
#                      원격 셸에서 전개되므로 $HOME/~ 사용 가능. on/off에만 필요.
#   --config-dir PATH  원격 설정 홈을 강제 지정(선택). 비우면 install 스크립트의
#                      기본 precedence(~/.claude-amx > ~/.claude)를 따른다.
#   --with-danger-hook `on`에서 위험명령 감지 PreToolUse 훅도 함께 설치(기본 off).
#                      원격 install-langfuse-hook.sh에 --with-danger-hook을 넘긴다.
#
# 동작 원칙
# --------
#   - ssh는 BatchMode=yes ConnectTimeout=5 로 붙는다(암호 프롬프트로 멈추지 않음).
#   - 한 호스트가 실패해도 나머지를 계속 진행하고, 끝에서 성공/실패를 집계한다.
#     실패가 하나라도 있으면 비정상 종료코드(1)로 끝낸다.
#   - `on` 시 시크릿은 원격 명령의 stdin으로 흘려 원격 명령 env에 채운다. 로컬·원격
#     어느 쪽의 argv(ps)에도 시크릿이 노출되지 않고, 이 스크립트도 절대 에코하지 않는다.
set -eu

SELF_DIR=$(cd "$(dirname "$0")" && pwd) || { echo "fleet-langfuse: cannot resolve script dir" >&2; exit 1; }

HOSTS_FILE="$SELF_DIR/fleet-hosts.txt"
REMOTE_REPO='$HOME/AMX'
CONFIG_DIR=""
SUBCMD=""
WITH_DANGER=0

die()  { echo "fleet-langfuse: $*" >&2; exit 1; }
info() { echo "fleet-langfuse: $*"; }

while [ $# -gt 0 ]; do
	case "$1" in
		on|off|status) SUBCMD="$1" ;;
		--hosts)       [ $# -ge 2 ] || die "--hosts needs a value"; shift; HOSTS_FILE="$1" ;;
		--remote-repo) [ $# -ge 2 ] || die "--remote-repo needs a value"; shift; REMOTE_REPO="$1" ;;
		--config-dir)  [ $# -ge 2 ] || die "--config-dir needs a value"; shift; CONFIG_DIR="$1" ;;
		--with-danger-hook) WITH_DANGER=1 ;;
		-h|--help)     sed -n '2,42p' "$0"; exit 0 ;;
		*) die "unknown argument: $1 (see --help)" ;;
	esac
	shift
done

[ -n "$SUBCMD" ] || die "missing subcommand (on|off|status); see --help"
[ -f "$HOSTS_FILE" ] || die "hosts file not found: $HOSTS_FILE (create it or pass --hosts)"

# 켜기엔 세 자격증명이 모두 필요하다(끄기/조회엔 불필요).
if [ "$SUBCMD" = on ]; then
	[ -n "${LANGFUSE_BASE_URL:-}" ]   || die "LANGFUSE_BASE_URL is required for 'on' (env)"
	[ -n "${LANGFUSE_PUBLIC_KEY:-}" ] || die "LANGFUSE_PUBLIC_KEY is required for 'on' (env)"
	[ -n "${LANGFUSE_SECRET_KEY:-}" ] || die "LANGFUSE_SECRET_KEY is required for 'on' (env)"
fi

SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=5"

# 원격 install 스크립트 경로와 --config-dir 인자(선택)를 조립.
REMOTE_SCRIPT="$REMOTE_REPO/deploy/install-langfuse-hook.sh"
CFG_ARG=""
[ -n "$CONFIG_DIR" ] && CFG_ARG="--config-dir '$CONFIG_DIR'"
DANGER_ARG=""
[ "$WITH_DANGER" = 1 ] && DANGER_ARG="--with-danger-hook"

# 한 호스트 처리. 성공 0 / 실패 비0. 시크릿은 여기서도 절대 출력하지 않는다.
run_on() {
	# 시크릿을 stdin 3줄로 흘려 원격 env에 채운다(argv 노출 없음).
	# shellcheck disable=SC2086
	printf '%s\n%s\n%s\n' "$LANGFUSE_BASE_URL" "$LANGFUSE_PUBLIC_KEY" "$LANGFUSE_SECRET_KEY" \
	| ssh $SSH_OPTS "$1" "IFS= read -r B; IFS= read -r P; IFS= read -r S; \
LANGFUSE_BASE_URL=\"\$B\" LANGFUSE_PUBLIC_KEY=\"\$P\" LANGFUSE_SECRET_KEY=\"\$S\" \
sh $REMOTE_SCRIPT $CFG_ARG $DANGER_ARG </dev/null"
}

run_off() {
	# shellcheck disable=SC2086
	ssh $SSH_OPTS "$1" "sh $REMOTE_SCRIPT --uninstall $CFG_ARG"
}

run_status() {
	# env 파일 존재 여부만 조회. --config-dir가 있으면 그 경로만, 없으면
	# install 스크립트와 같은 precedence로 첫 존재 경로를 보고한다.
	# shellcheck disable=SC2086
	ssh $SSH_OPTS "$1" "CFG='$CONFIG_DIR'; \
for d in \"\$CFG\" \"\$HOME/.claude-amx\" \"\$HOME/.claude\"; do \
  [ -n \"\$d\" ] || continue; \
  if [ -f \"\$d/amx-langfuse.env\" ]; then echo \"ON   \$d/amx-langfuse.env\"; exit 0; fi; \
  [ -n \"\$CFG\" ] && { echo \"OFF  \$CFG (no amx-langfuse.env)\"; exit 0; }; \
done; echo 'OFF  (no amx-langfuse.env in ~/.claude-amx or ~/.claude)'"
}

OK_N=0
FAIL_N=0
FAILED=""

info "subcommand=$SUBCMD  hosts=$HOSTS_FILE"
while IFS= read -r line || [ -n "$line" ]; do
	# 앞뒤 공백 제거 후 빈 줄·주석 건너뛰기.
	host=$(printf '%s' "$line" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
	case "$host" in
		''|\#*) continue ;;
	esac

	printf -- '---- %s ----\n' "$host"
	rc=0
	case "$SUBCMD" in
		on)     run_on "$host"     || rc=$? ;;
		off)    run_off "$host"    || rc=$? ;;
		status) run_status "$host" || rc=$? ;;
	esac

	if [ "$rc" -eq 0 ]; then
		OK_N=$((OK_N + 1))
	else
		FAIL_N=$((FAIL_N + 1))
		FAILED="$FAILED $host(rc=$rc)"
		echo "fleet-langfuse: $host FAILED (rc=$rc) — 계속 진행" >&2
	fi
done < "$HOSTS_FILE"

echo
info "요약: 성공 $OK_N · 실패 $FAIL_N"
if [ "$FAIL_N" -gt 0 ]; then
	info "실패 호스트:$FAILED"
	exit 1
fi
info "전 호스트 완료"
