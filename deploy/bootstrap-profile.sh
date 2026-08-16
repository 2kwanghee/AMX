#!/bin/sh
# bootstrap-profile.sh — 겸용 PC에서 개인 Claude 프로필의 유저 스코프 자산을
# AMX 러너 프로필에 연결한다. 멱등(여러 번 돌려도 같은 상태로 수렴).
#
# 무엇을 / 왜
# ----------
# 한 대의 PC를 개인 작업(`~/.claude`)과 AMX 러너(`~/.claude-amx`)가 함께 쓰면,
# 개인 프로필에 쌓아 둔 전역 지침(CLAUDE.md)·스킬·키바인딩·에이전트·명령·프로젝트
# 메모리가 러너(amx) 세션에서는 보이지 않는다. 이 스크립트는 그 유저 스코프 자산을
# 러너 프로필에서 심볼릭 링크로 참조하게 만들어, 두 프로필이 같은 지침·스킬·메모리를
# 공유하도록 한다. 자격증명·대화 이력·settings.json 은 공유하지 않는다(§ 아래).
#
# 안전 규약 (설계 제약 — 위반 금지)
# ---------------------------------
#   1. 이 스크립트는 러너 홈(--config-dir) 안에만 파일(링크)을 만든다. tsamx가
#      만드는 세션 프로필이나 개인 프로필(--personal-dir) 내부에는 어떤 파일도
#      쓰지 않는다 — 개인 프로필은 스크립트 실행 시점 기준으로는 읽기 전용이다.
#      이유: tsamx의 history 병합이 shutil.move로 프로필 projects/ 를 옮기므로,
#      그 경로 안에 심링크가 있으면 원본이 유실될 수 있다. 러너 홈은 그 이관
#      경로 밖이라 안전하다.
#
#      단, 이 "읽기 전용"은 설치 시점 이야기일 뿐이다. 링크가 걸린 뒤 런타임
#      동작은 다르다:
#        - 프로젝트 메모리 링크는 양방향 write 공유다. 러너의 자동화 세션이
#          projects/<slug>/memory 에 쓰면 그 내용이 개인 프로필의 실제
#          메모리 파일에 그대로 반영된다(오염 가능). 개인·러너를 격리하려면
#          해당 slug는 링크에서 빼거나 uninstall로 끊어야 한다.
#        - 링크된 개인 지침(CLAUDE.md)·스킬은 러너 세션의 실제 동작에 적용된다.
#        - 개인 메모리 내용은 러너 세션이 읽어 Langfuse 트레이스로 나갈 수 있다.
#   2. settings.json 은 링크하지 않는다(AMX Langfuse 훅이 이 파일에 병합 기록을
#      남겨 개인 원본을 오염시킨다). 러너 쪽에 없을 때만 복사하고, 있으면 무변경.
#   3. 러너 쪽에 이미 실파일/실디렉(사용자 데이터)이 있으면 건드리지 않고 경고만
#      한다. 이미 올바른 링크면 무변경, 깨진 링크면 재생성.
#
# 사용법
# ------
#   deploy/bootstrap-profile.sh                 # 기본값으로 연결(메모리 포함)
#   deploy/bootstrap-profile.sh --personal-dir ~/.claude --config-dir ~/.claude-amx
#   deploy/bootstrap-profile.sh --no-memory     # 프로젝트 메모리 링크 단계 전체 생략
#   deploy/bootstrap-profile.sh --uninstall     # 이 스크립트가 만든 링크만 제거
#
# 각 항목 처리 결과를 linked / copied / skipped / warned 한 줄씩 출력한다.

set -eu

PERSONAL_DIR="$HOME/.claude"
CONFIG_DIR="$HOME/.claude-amx"
UNINSTALL=0
NO_MEMORY=0

while [ $# -gt 0 ]; do
	case "$1" in
		--personal-dir) shift; PERSONAL_DIR="${1:-}" ;;
		--config-dir)   shift; CONFIG_DIR="${1:-}" ;;
		--no-memory)    NO_MEMORY=1 ;;
		--uninstall)    UNINSTALL=1 ;;
		-h|--help)      sed -n '2,42p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) echo "bootstrap-profile: 알 수 없는 인자: $1 (--help 참고)" >&2; exit 1 ;;
	esac
	shift
done

# 링크 원본이 cwd에 의존하지 않도록 절대경로로 고정한다.
abspath() {
	case "$1" in
		/*) printf '%s\n' "$1" ;;
		*)  printf '%s/%s\n' "$(pwd)" "$1" ;;
	esac
}
PERSONAL_DIR="$(abspath "$PERSONAL_DIR")"
CONFIG_DIR="$(abspath "$CONFIG_DIR")"

info() { printf '  %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }

# 이 스크립트의 위치(amx 원본을 여기서 찾는다).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 링크 대상이 개인 프로필을 가리키는지 판정(깨진 링크도 판정 가능하도록 readlink 1단계).
points_into_personal() {
	# $1 = 링크 경로
	[ -L "$1" ] || return 1
	_t="$(readlink "$1")"
	case "$_t" in
		"$PERSONAL_DIR"/*|"$PERSONAL_DIR") return 0 ;;
		*) return 1 ;;
	esac
}

# ── 공통: 심볼릭 링크 1건 처리 (설치) ────────────────────────────────────────
# $1 = 프로필 기준 상대 경로(예: CLAUDE.md, skills, projects/<slug>/memory)
link_one() {
	_rel="$1"
	_src="$PERSONAL_DIR/$_rel"
	_dst="$CONFIG_DIR/$_rel"

	# 원본이 개인 프로필에 없으면 아무것도 하지 않는다.
	if [ ! -e "$_src" ] && [ ! -L "$_src" ]; then
		info "skipped $_rel (개인 프로필에 없음)"
		return 0
	fi

	if [ -L "$_dst" ]; then
		_cur="$(readlink "$_dst")"
		if [ "$_cur" = "$_src" ]; then
			info "unchanged $_rel (이미 링크됨)"
			return 0
		fi
		if [ ! -e "$_dst" ] || points_into_personal "$_dst"; then
			# 깨진 링크이거나 개인 프로필을 가리키는 옛 링크 → 재생성
			rm -f "$_dst"
			ln -s "$_src" "$_dst"
			info "linked $_rel (재생성)"
			return 0
		fi
		warn "skipped $_rel (러너 쪽 링크가 외부를 가리킴: $_cur — 보호)"
		return 0
	fi

	if [ -e "$_dst" ]; then
		warn "skipped $_rel (러너 쪽 실제 파일/디렉터리 존재 — 사용자 데이터 보호)"
		return 0
	fi

	ln -s "$_src" "$_dst"
	info "linked $_rel"
}

# ── 프로젝트 메모리 1건 처리 (빈 실디렉이면 교체, 비어 있지 않으면 보호) ──────
link_memory() {
	_rel="$1"           # projects/<slug>/memory
	_src="$PERSONAL_DIR/$_rel"
	_dst="$CONFIG_DIR/$_rel"
	_parent="$(dirname "$_dst")"
	mkdir -p "$_parent"

	if [ -L "$_dst" ]; then
		_cur="$(readlink "$_dst")"
		if [ "$_cur" = "$_src" ]; then
			info "unchanged $_rel (이미 링크됨)"
			return 0
		fi
		if [ ! -e "$_dst" ] || points_into_personal "$_dst"; then
			rm -f "$_dst"
			ln -s "$_src" "$_dst"
			info "linked $_rel (재생성)"
			return 0
		fi
		warn "skipped $_rel (러너 쪽 링크가 외부를 가리킴: $_cur — 보호)"
		return 0
	fi

	if [ -d "$_dst" ]; then
		# 비어 있으면 교체, 비어 있지 않으면 보호. ls -A는 비POSIX라 find로 판정
		# (숨김 파일 포함, 첫 항목에서 멈춤).
		if [ -z "$(find "$_dst" -mindepth 1 -print -quit 2>/dev/null)" ]; then
			rmdir "$_dst" && ln -s "$_src" "$_dst"
			info "linked $_rel (빈 디렉터리 교체)"
		else
			warn "skipped $_rel (러너 쪽에 비어 있지 않은 메모리 존재 — 보호)"
		fi
		return 0
	fi

	if [ -e "$_dst" ]; then
		warn "skipped $_rel (러너 쪽에 동명 실파일 존재 — 보호)"
		return 0
	fi

	ln -s "$_src" "$_dst"
	info "linked $_rel"
}

# ── settings.json: 없을 때만 복사 (링크 금지) ────────────────────────────────
copy_settings() {
	_src="$PERSONAL_DIR/settings.json"
	_dst="$CONFIG_DIR/settings.json"
	if [ ! -f "$_src" ]; then
		info "skipped settings.json (개인 프로필에 없음)"
		return 0
	fi
	if [ -e "$_dst" ] || [ -L "$_dst" ]; then
		info "unchanged settings.json (러너 쪽 존재 — 무변경, Langfuse 훅 오염 방지)"
		return 0
	fi
	# 링크가 아니라 복사. 0600 유지.
	cp "$_src" "$_dst"
	chmod 600 "$_dst"
	info "copied settings.json (0600)"
}

# ── amx 명령을 bin 디렉터리에 설치 (~/.local/bin 존재 시) ─────────────────────
install_amx_cmd() {
	_bin="$HOME/.local/bin"
	_src="$SCRIPT_DIR/amx"
	if [ ! -f "$_src" ]; then
		warn "amx 원본을 찾지 못함 ($_src) — 명령 설치 생략"
		return 0
	fi
	if [ ! -d "$_bin" ]; then
		info "skipped amx (~/.local/bin 없음) — 직접 PATH에 두려면: cp $_src <bin>"
		return 0
	fi
	# 설치 사본에 이 서버의 config dir을 각인한다(비기본 홈 디커플링). sed 치환
	# 안전을 위해 CONFIG_DIR의 sed 특수문자(& | \)를 이스케이프한다. 런타임
	# AMX_CONFIG_DIR override는 amx 원본에 그대로 남아 있어 유지된다.
	_esc="$(printf '%s' "$CONFIG_DIR" | sed 's/[&|\\]/\\&/g')"
	sed "s|^AMX_DEFAULT_CONFIG_DIR=.*|AMX_DEFAULT_CONFIG_DIR=\"$_esc\"|" "$_src" > "$_bin/amx"
	chmod +x "$_bin/amx"
	info "copied amx ($_bin/amx, config dir=$CONFIG_DIR)"
	# amx는 같은 디렉터리(우선) 또는 PATH에서 amx-claude 래퍼를 찾는다. 사본만
	# 설치하면 래퍼를 못 찾아 즉시 실패하므로 래퍼도 나란히 설치한다(라이브
	# 테스트에서 실측된 결함). 저장소 원본이 없으면 경고만.
	if [ -f "$SCRIPT_DIR/amx-claude" ]; then
		cp "$SCRIPT_DIR/amx-claude" "$_bin/amx-claude"
		chmod +x "$_bin/amx-claude"
		info "copied amx-claude ($_bin/amx-claude)"
	else
		warn "amx-claude 원본을 찾지 못함 ($SCRIPT_DIR/amx-claude) — amx가 PATH의 amx-claude에 의존합니다"
	fi
	case ":$PATH:" in
		*":$_bin:"*) ;;
		*) info "안내: $_bin 이 PATH에 없습니다 — 추가하면 어디서든 'amx'로 실행됩니다." ;;
	esac
}

# ── 개인 프로필의 프로젝트 메모리 slug 목록 나열 ─────────────────────────────
# projects/<slug>/memory 형태의 실디렉만 대상으로 한다.
each_personal_memory() {
	_glob="$PERSONAL_DIR/projects/"*"/memory"
	for _m in $_glob; do
		[ -d "$_m" ] || continue           # glob 미매치 시 리터럴 스킵
		_slug="$(basename "$(dirname "$_m")")"
		printf 'projects/%s/memory\n' "$_slug"
	done
}

# ── uninstall: 이 스크립트가 만든 링크(개인 프로필을 가리키는 심링크)만 제거 ──
do_uninstall() {
	echo "제거 대상: $CONFIG_DIR 안에서 개인 프로필($PERSONAL_DIR)을 가리키는 심링크만"
	for _rel in CLAUDE.md skills keybindings.json agents commands; do
		_dst="$CONFIG_DIR/$_rel"
		if points_into_personal "$_dst"; then
			rm -f "$_dst"
			info "removed $_rel (링크)"
		elif [ -L "$_dst" ]; then
			info "kept $_rel (외부를 가리키는 링크 — 보존)"
		elif [ -e "$_dst" ]; then
			info "kept $_rel (실파일/실디렉 — 보존)"
		fi
	done
	# 프로젝트 메모리 링크
	if [ -d "$CONFIG_DIR/projects" ]; then
		for _mp in "$CONFIG_DIR/projects/"*"/memory"; do
			[ -L "$_mp" ] || continue
			_rel="projects/$(basename "$(dirname "$_mp")")/memory"
			if points_into_personal "$_mp"; then
				rm -f "$_mp"
				info "removed $_rel (링크)"
			else
				info "kept $_rel (외부를 가리키는 링크 — 보존)"
			fi
		done
	fi
	info "settings.json / amx / 복사본·실파일은 보존했습니다."
	echo "제거 끝."
}

# ── main ─────────────────────────────────────────────────────────────────────
if [ ! -d "$PERSONAL_DIR" ]; then
	echo "bootstrap-profile: 개인 프로필이 없습니다 ($PERSONAL_DIR) — 할 일이 없어 종료." >&2
	exit 0
fi

if [ "$UNINSTALL" = 1 ]; then
	do_uninstall
	exit 0
fi

echo "개인 프로필: $PERSONAL_DIR"
echo "러너 프로필: $CONFIG_DIR"
mkdir -p "$CONFIG_DIR"

# 1) 유저 스코프 자산 링크
for rel in CLAUDE.md skills keybindings.json agents commands; do
	link_one "$rel"
done

# 2) 프로젝트 메모리 링크 (개인 projects/*/memory 각각)
#    --no-memory 시 이 단계 전체를 건너뛴다(무인 러너의 개인 메모리 오염 표면 제거).
if [ "$NO_MEMORY" = 1 ]; then
	info "skipped projects/*/memory (--no-memory — 개인 메모리 링크 생략)"
else
	each_personal_memory | while IFS= read -r rel; do
		link_memory "$rel"
	done
fi

# 3) settings.json (없을 때만 복사)
copy_settings

# 4) amx 명령 설치
install_amx_cmd

echo "부트스트랩 끝. amx 세션은 개인 프로필의 지침·스킬·메모리를 공유합니다."
