# tsamx 재설계 타당성 검토 (2026-08-22)

tsamx를 cswap(claude-swap)의 파생이 아니라 AMX 고유 메커니즘으로 다시 만들 수
있는가에 대한 검토다. 조사 2건(tsamx 내부·상류 의존, AMX 연동 계약면)의 결론 위에
선다. 결론부터 말하면 **가능하고, 생각보다 제약이 적다.** 지켜야 할 것은 바깥
계약 17개뿐이고 안쪽은 전부 갈아엎어도 된다.

## 지금의 tsamx가 무엇인지

- claude-swap v0.25.0b1의 MIT 포크다. 32개 모듈 중 26개가 rename 외 0줄 차이이고
  AMX가 덧붙인 코드는 `switcher.py` 190줄, `cli.py` 48줄 정도다. 상류 동기화는
  초기 스냅샷(2026-08-08) 이후 한 번도 없었다.
- 2만 1천 줄 중 AMX가 쓰지 않는 부분이 크다. Textual TUI(1.7천), 메뉴바(952),
  macOS 키체인, 세션 모드(`tsamx run`, 1.1천), 이전/내보내기(617).
- 전환 메커니즘은 라이브 파일 스왑이다. `<CLAUDE_CONFIG_DIR>/.credentials.json`과
  `~/.claude.json`의 `oauthAccount`를 백업 슬롯과 맞바꾸고 `sequence.json`의
  `activeAccountNumber`를 갱신한다(switcher.py:5184-5712). 나가는 자격증명이
  남의 것(foreign/alien)이거나 Claude Code가 refresh 실패로 비워버린(wiped)
  상태인지 분류하는 방어 코드가 이 구조의 복잡도 대부분을 차지한다.
- 구조적 제약: 활성 계정이 한 번에 하나라는 가정이 40여 곳에 박혀 있어 테넌트
  분리가 막혀 있고, CLI는 서브커맨드 파서가 아니라 플래그 변환 테이블이라
  `auto`/`config`가 첫 인자여야 하며, Codex 지원은 없다. 라이선스는 MIT라
  재작성·대체에 법적 제약은 없다.

## 재작성 시 반드시 지켜야 할 계약

에이전트·래퍼·서버·e2e가 실제로 파싱하거나 전제하는 것만 추린 목록이다.

| 구분 | 계약 | 소비자 |
|---|---|---|
| CLI 동사 | `add`(인자 없음, config home의 자격증명 캡처), `remove <email> --yes`, `enable/disable <email>`, `switch <email>`, `switch --strategy best\|next-available`, `config set autoswitch.threshold\|cooldownSeconds\|hysteresisPct <n>` | ama-agent exec.go |
| 종료코드 | `auto --once`: 0 전환됨 / 1 오류 / 2 무조치 / 3 전원 소진 | scheduler |
| JSON | `list --json` v1: `schemaVersion, activeAccountNumber, accounts[]{number,email,organizationName,organizationUuid,active,disabled,usageStatus,usage{fiveHour,sevenDay{pct,resetsAt},spend,scoped[]},usageFetchedAt,alias}` | reporter |
| 리터럴 | `usageStatus=="relogin_required"` = 격리, `usage==null` = 미측정, `disabled=true` = 로테이션 제외 | reporter(PoolSummary) |
| 동기성 | `switch/add/enable` 반환 시점에 풀 상태 확정, 직후 `list`가 새 active를 반환 | handlers |
| `status --json` | 최상위 `active.email` | amx-claude 래퍼, Langfuse 훅 |
| 상태 파일 | `$XDG_DATA_HOME/tsamx/autoswitch_state.json`(Windows `~/.tsamx-backup`)의 `quarantine{slot:{email}}`, 원자적 rename 쓰기 | scheduler fsnotify |
| 락 | `<CLAUDE_CONFIG_DIR>/.amx-deliver.lock`은 에이전트·래퍼 전용, tsamx는 건드리지 않음 | agent ↔ 러너 래퍼 |
| config home | `CLAUDE_CONFIG_DIR`로 풀 홈 주입, `<home>/.credentials.json`, `<home>/.claude.json.oauthAccount` | agent가 tsamx 없이도 직접 읽고 씀 |
| 지문 | `sha256:hex(refreshToken)` 규칙 동치 | resync(Go 쪽에 복제본 존재) |
| 설치 | `uv tool install --from <repo>/tsamx tsamx`, `~/.local/bin/tsamx`, self-update가 바이너리 교체 직전 재설치 | agent-setup.sh, selfupdate.go |
| e2e 캐시 | `$XDG_DATA_HOME/tsamx/cache/usage.json` schemaVersion 2 | e2e conftest |

바꿔도 되는 것은 그 외 전부다 — 내부 구현, 사람용 출력, TUI·메뉴바·키체인, 에이전트가
읽지 않는 JSON 필드(pace/countdown/expectedPct 등), `run`·`upstream` 같은 운영자용
서브커맨드. 주의할 빈자리 하나: **에이전트와 tsamx 사이에 버전 협상이 없다.** proto의
`tsamx_version` 필드는 에이전트가 채우지 않고 `schemaVersion`도 검증하지 않아,
재작성본이 구버전 에이전트를 만나면 조용히 오작동한다. 재설계에서 이 공백을 먼저
메워야 한다.

## 가능한 메커니즘

### A. 에이전트 내장 Go 모듈 (형태의 변경)

계정 관리 로직을 별도 Python CLI가 아니라 ama-agent 안의 Go 패키지로 옮기고,
운영자용 얇은 CLI만 남긴다. 에이전트는 이미 자격증명 파일을 직접 읽고 쓰며
(claude.go:84-126) 지문 알고리즘도 복제해 갖고 있으니, 지금은 같은 일을 두 언어로
두 번 하는 셈이다. 이 형태로 가면 uv/Python 런타임 의존, 설치·업데이트 이중
경로(C16), 버전 불일치 문제가 구조적으로 사라진다. 풀링 예산 정책(60분 슬라이딩
~28건, AIMD)은 tsamx의 `poll_policy.py`에서 그대로 이식한다 — 이건 관측으로 얻은
지식이라 버리면 안 된다.

### B. 프로파일 디렉터리 전환 (메커니즘의 변경)

계정마다 독립된 `CLAUDE_CONFIG_DIR`을 두고, 전환을 "라이브 파일 맞바꾸기"가 아니라
"래퍼가 가리키는 디렉터리 바꾸기"로 정의한다. 러너 래퍼(amx-claude, tsclaude)가
이미 `CLAUDE_CONFIG_DIR`를 주입하고 있어 자연스럽게 붙는다. 이렇게 하면 foreign/alien/
wiped 분류, Claude Code 자체 락과의 경합, 백업 슬롯 무결성 같은 현행 복잡도의
뿌리가 사라지고, 단일 활성 계정 가정도 깨진다 — 테넌트·프로바이더별로 여러 계정을
동시에 "살아 있는" 상태로 둘 수 있다. 대가는 설정 공유(프로파일마다 settings·
projects가 갈리므로 공통 설정을 심볼릭 링크나 템플릿으로 묶어야 함)와, 이미 떠
있는 Claude Code 세션은 다음 실행부터 바뀐다는 점이다(현행도 다음 메시지부터
반영이라 체감 차이는 작다).

### C. 자격증명 브로커 — 채택 불가

`apiKeyHelper` 훅이나 로컬 프록시로 토큰을 주입하는 방식은 API 키 계정에만 통하고
OAuth 구독 계정은 `.credentials.json`을 요구한다. AMX의 주 대상이 구독 계정이므로
제외한다.

## 권고

A와 B를 함께 간다. 형태는 Go 내장 모듈, 메커니즘은 프로파일 디렉터리. 위 계약
17개는 이행 기간 동안 호환 CLI(`tsamx` 이름·동사·JSON v1)로 그대로 제공하고,
에이전트가 같은 바이너리에서 내부 호출로 전환하면 CLI 계약은 운영자용으로만
남긴다. Codex는 처음부터 프로바이더 인터페이스의 두 번째 구현으로 넣는다
(현재 세션 로그 기반 수동 수집이 bridge.go에 이미 있다).

## 결정 대기 사항

1. 형태 — Go 내장(권고) / 독립 Python 재작성. Python을 고르면 런타임·설치 이중
   경로가 남는다.
2. 메커니즘 — 프로파일 디렉터리(권고) / 파일 스왑 유지. 스왑을 유지하면 현행
   방어 코드를 사실상 다시 써야 한다.
3. 호환 기간 — 기존 CLI 계약을 얼마나 오래 유지할지. 에이전트와 함께 배포되므로
   동시 교체도 가능하나 러너 래퍼·Langfuse 훅·e2e가 걸려 있다.
4. 플랫폼 — Linux/WSL 필수. Windows 네이티브(keyring)·macOS 키체인 지원 여부.
5. 프로바이더 — Codex 1급 포함 여부와 우선순위.

결정이 모이면 단계별 계획(계약 고정 테스트 → 프로바이더 인터페이스 → 프로파일
스토어 → 전환·자동화 엔진 → 호환 CLI → 이행)을 별도 기획서로 낸다.
