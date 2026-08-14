#!/usr/bin/env bash
# build-artifacts.sh — 패키지형 설치용 릴리스 산출물을 dist/ 에 생산한다.
#
# 대상 머신에 git·go·python 이 없어도 에이전트를 설치할 수 있도록, dev PC 에서
# 미리 크로스컴파일한 ama 바이너리와 tsamx wheel 을 dist/ 에 모아 둔다.
#   - 이 스크립트(PR1)  : dist/ 산출물 + manifest.json 생산(서명 없음)
#   - AMS 서빙(PR2)     : dist/ 를 HTTP 로 서빙하며 자기 키로 서명 추가
#   - install.sh(PR3)   : 대상 머신에서 산출물을 내려받아 설치
#
# ldflags 의 `-X main.commit=<sha>` 는 deploy/agent-run.sh 및 self_update 재빌드와
# 동일한 관례다(cmd/ama/main.go 의 var commit). 덕분에 `--version` 이 찍는 커밋
# 문자열이 세 경로에서 모두 일치해 self_update 스모크(--version 커밋 검사)와 정합한다.
#
# 사용법:
#   deploy/build-artifacts.sh                       # 기본 3종 + wheel + manifest
#   deploy/build-artifacts.sh --targets linux-amd64 # 일부 타깃만 (콤마 구분)
#   deploy/build-artifacts.sh --targets linux-amd64,windows-amd64
#
# 재실행 시 dist/ 를 비우고 다시 만든다(멱등). 어떤 단계든 실패하면 즉시 중단한다.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/dist"

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_dim=$'\033[2m'; c_rst=$'\033[0m'
ok()   { printf '%s✔%s %s\n' "$c_grn" "$c_rst" "$*"; }
step() { printf '%s· %s%s\n' "$c_dim" "$*" "$c_rst"; }
err()  { printf '%sx%s %s\n' "$c_red" "$c_rst" "$*" >&2; }
die()  { err "$*"; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "필요한 명령이 없습니다: $1"; }

# ── 타깃 목록 ─────────────────────────────────────────────────────────────────
# "goos-goarch" 표기. 기본은 3종 전체. --targets 로 부분 빌드 허용.
ALL_TARGETS="linux-amd64 linux-arm64 windows-amd64"
TARGETS="$ALL_TARGETS"
while [ $# -gt 0 ]; do
  case "$1" in
    --targets) TARGETS="$(printf '%s' "$2" | tr ',' ' ')"; shift 2 ;;
    -h|--help) sed -n '2,29p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "알 수 없는 인자: $1" ;;
  esac
done

# 알 수 없는 타깃은 조용히 넘어가지 않고 즉시 실패시킨다.
for t in $TARGETS; do
  case " $ALL_TARGETS " in
    *" $t "*) ;;
    *) die "알 수 없는 타깃: $t (가능: $ALL_TARGETS)" ;;
  esac
done

need go
need git
need sha256sum

SHA="$(git -C "$ROOT" rev-parse HEAD)" || die "git HEAD 를 읽을 수 없습니다"
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ── dist/ 정리 후 재생산(멱등) ────────────────────────────────────────────────
step "dist/ 정리"
rm -rf "$DIST"
mkdir -p "$DIST"

# sha256/size 를 계산해 매니페스트 조각(JSON object 멤버)을 표준출력으로 낸다.
MANIFEST_ITEMS=()
record_artifact() {
  local f="$1" name sum size
  name="$(basename "$f")"
  sum="$(sha256sum "$f" | cut -d' ' -f1)"
  size="$(wc -c < "$f" | tr -d ' ')"
  MANIFEST_ITEMS+=("    \"$name\": { \"sha256\": \"$sum\", \"size\": $size }")
}

# ── 1) ama 바이너리 크로스컴파일 ──────────────────────────────────────────────
# ama-agent 는 CGO 미사용이라 CGO_ENABLED=0 로 자명하게 크로스컴파일된다.
# -trimpath 로 빌드 경로를 지우고, -X main.commit 으로 커밋을 새긴다.
for t in $TARGETS; do
  goos="${t%-*}"; goarch="${t#*-}"
  out="$DIST/ama-$t"
  [ "$goos" = "windows" ] && out="$out.exe"
  step "빌드 ama $t"
  ( cd "$ROOT/ama-agent" && CGO_ENABLED=0 GOOS="$goos" GOARCH="$goarch" \
      go build -trimpath -ldflags "-X main.commit=$SHA" -o "$out" ./cmd/ama ) \
    || die "go build 실패: $t"
  record_artifact "$out"
  ok "ama-$t"
done

# ── 2) tsamx wheel ────────────────────────────────────────────────────────────
# tsamx 는 순수 파이썬(uv 프로젝트)이라 플랫폼 무관 wheel 하나면 된다. dev PC 에는
# uv 가 있으므로 uv build 를 쓰고, 없으면 python -m build 로 폴백한다.
step "빌드 tsamx wheel"
if command -v uv >/dev/null 2>&1; then
  ( cd "$ROOT/tsamx" && uv build --wheel --out-dir "$DIST" ) || die "uv build 실패"
elif command -v python3 >/dev/null 2>&1 && python3 -m build --help >/dev/null 2>&1; then
  ( cd "$ROOT/tsamx" && python3 -m build --wheel --outdir "$DIST" ) || die "python -m build 실패"
else
  die "wheel 을 만들 도구가 없습니다: uv 또는 python -m build 가 필요합니다"
fi

WHEEL="$(ls -1 "$DIST"/tsamx-*.whl 2>/dev/null | head -n1)"
[ -n "$WHEEL" ] || die "wheel 산출물을 찾을 수 없습니다"
# 안정 이름 심링크는 사람이 손으로 집을 때의 편의용일 뿐이다(심링크 미지원 환경
# 대비 복사 폴백). 심링크 자체는 매니페스트에 없으므로 **서명으로 보호되지 않는다**
# — install.sh 는 절대 tsamx-latest.whl 을 받으면 안 되고, 매니페스트의
# version.wheel 이 가리키는 실파일명을 받아 sha256 을 대조해야 한다. 서명 밖 파일을
# 설치 경로에 두면 그 파일 교체만으로 임의 파이썬 코드가 실행된다.
ln -sf "$(basename "$WHEEL")" "$DIST/tsamx-latest.whl" 2>/dev/null \
  || cp -f "$WHEEL" "$DIST/tsamx-latest.whl"
record_artifact "$WHEEL"
WHEEL_NAME="$(basename "$WHEEL")"
ok "$(basename "$WHEEL")  (→ tsamx-latest.whl)"

# ── 3) manifest.json ──────────────────────────────────────────────────────────
# 서명은 여기서 하지 않는다 — PR2 에서 AMS 가 자기 키로 dist/ 를 서빙하며 서명한다.
step "manifest.json 생성"
{
  printf '{\n'
  # wheel: 서명 대상인 실파일명. install.sh 가 버전을 추측하거나 서명 밖의
  # tsamx-latest.whl 로 폴백하지 않도록 매니페스트 안에서 정본을 지목한다.
  printf '  "version": { "commit": "%s", "builtAt": "%s", "wheel": "%s" },\n' \
    "$SHA" "$BUILT_AT" "$WHEEL_NAME"
  printf '  "artifacts": {\n'
  local_n=${#MANIFEST_ITEMS[@]}
  for i in "${!MANIFEST_ITEMS[@]}"; do
    if [ "$i" -lt $((local_n - 1)) ]; then
      printf '%s,\n' "${MANIFEST_ITEMS[$i]}"
    else
      printf '%s\n' "${MANIFEST_ITEMS[$i]}"
    fi
  done
  printf '  }\n'
  printf '}\n'
} > "$DIST/manifest.json"
ok "manifest.json ($local_n artifacts)"

# ── 매니페스트 sha256 자체 검증 ───────────────────────────────────────────────
# 기록한 sha256 이 실측과 일치하는지 되읽어 확인한다(완료조건 3).
step "sha256 검증"
while IFS= read -r line; do
  name="$(printf '%s' "$line" | sed -n 's/.*"\([^"]*\)": { "sha256".*/\1/p')"
  want="$(printf '%s' "$line" | sed -n 's/.*"sha256": "\([0-9a-f]*\)".*/\1/p')"
  [ -n "$name" ] || continue
  got="$(sha256sum "$DIST/$name" | cut -d' ' -f1)"
  [ "$got" = "$want" ] || die "sha256 불일치: $name (manifest=$want actual=$got)"
done < "$DIST/manifest.json"
ok "sha256 전부 일치"

printf '\n'
ok "완료 → $DIST"
ls -la "$DIST"
