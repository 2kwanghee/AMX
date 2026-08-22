# 시트 엔진 재설계 기획서 — tsamx 후속 (2026-08-22)

tsamx(claude-swap 포크)를 대체할 AMX 고유 메커니즘의 단계별 계획이다. 타당성
검토(`tsamx-rewrite-feasibility.md`)의 권고를 그대로 확정했다: 형태는 ama-agent
내장 Go 모듈, 메커니즘은 계정별 프로파일 디렉터리, 기존 계약은 호환층으로
유지. 여기에 프로바이더별 심층 리서치(Codex·Gemini·Grok·기타, 2026-08-22) 결과를
반영해 관리 대상 범위와 법적 경계를 먼저 못 박는다.

## 0. 불변 원칙

1. **기존 tsamx는 제거하지 않는다.** 새 엔진은 에이전트 안에서 플래그로 선택되는
   두 번째 브리지 구현이고, tsamx 브리지는 그대로 남아 서버별로 언제든 되돌릴 수
   있다. 폐기는 새 엔진이 전 플릿에서 충분히 검증된 뒤 사용자가 결정한다.
2. **이름 충돌을 만들지 않는다.** 새 엔진의 운영자 CLI는 `ama-agent seat …`
   (또는 `amx seat …`)이며 `tsamx` 바이너리 이름을 쓰지 않는다. 에이전트는 새
   엔진을 프로세스 실행이 아니라 Go 내부 호출로 쓴다.
3. **사람 경계를 넘는 로테이션은 기본값으로 막는다.** 아래 1절의 법적 판정이
   모든 프로바이더에서 같은 결론을 내므로, 계정-소유자 바인딩과 소유자 범위
   로테이션 정책을 엔진의 1급 개념으로 둔다.

## 1. 프로바이더 판정 (리서치 결과)

리서치는 공식 문서·약관 원문을 직접 확인한 것만 근거로 삼았고, 확인 못 한
항목은 그대로 "확인 불가"로 남겼다. 블로그·SEO 출처의 단속 사례는 채택하지
않았다.

| 프로바이더 | 기술 | 약관 | AMX 범위 |
|---|---|---|---|
| Claude Code (현행) | 가능 | 소비자 약관 "계정·자격증명을 타인에게 제공 금지", 이용정책 "다중 계정으로 가드레일 우회 금지" | 1급. 단 **소유자 경계 안 로테이션**으로 정책 전환 필요 |
| Grok Build (xAI 공식 CLI) | 가능 — `GROK_HOME`, `~/.grok/auth.json`, `grok login/logout`, device-auth | Anthropic과 사실상 같은 문언. 엔터프라이즈 `force_login_team_uuid`로 팀 고정 가능 | 2순위, Claude와 같은 드라이버 구조 |
| Codex (OpenAI) | 가능 — `CODEX_HOME`, `auth.json`, 내부 `wham/usage` 엔드포인트 | OSA §3.1 자격증명 다중 사용자 공유 금지, §3.2 계정은 단일 엔드유저, §3.3(i) **"사용 한도를 피하도록 서비스를 구성" 금지**. CI/CD 공식 지침 "auth.json을 여러 머신에서 공유하지 말 것"(refresh 회전) | 3순위. **배포·회수·가시성만, 자동 전환 제외**(기존 CodexBridge 유지). 한 번에 한 보유처(이동식) |
| Gemini CLI | 가능하되 전제 붕괴 — **개인 구독(무료·AI Pro·Ultra) 경로가 2026-06-18 종료**, Code Assist Standard/Enterprise만 잔존 | GCP 약관 §3.3(d)(iii) "할당량 우회 목적의 다중 계정" 명시 금지, 서드파티 도구 접근 밴 선례(2차 위반 영구, Gemini CLI·Code Assist 동반 차단) | **자격증명 스위칭 방식 채택 불가.** 정합적 경로는 라이선스 assign/unassign API + Cloud Logging 사용량인데 이는 다른 종류의 통합이라 본 계획 밖(보류) |
| GitHub Copilot CLI | 가능 | 시트당 1인, 조직 시트 API 정식 | 후보(4순위). 조직 시트 API로 회수·재배급 정식 경로 가능 |
| Kimi Code·Cursor CLI·Mistral Vibe | 가능 | 구독 한도형, 시트당 1인 | 후보군, 우선순위 낮음 |
| Amazon Kiro CLI | 가능 | AWS SSO 기반 조직 프로비저닝이 정식 | AMX 없이도 되므로 가치 낮음 |
| OpenCode·Crush·Qwen Code | — | API 키 종량제(Qwen은 무료 OAuth 종료) | "계정 스위칭" 개념 무의미, 제외 |

**보고해야 할 법적 결론 두 가지.**

첫째, Gemini는 "Gemini끼리 스위칭"이 기술적으로는 되지만 약관이 자동 전환을
정면으로 겨냥하고 밴 선례가 조직 단위로 번진다. 자격증명 복사·로테이션 방식은
채택하지 않는다.

둘째, Codex·Grok·Anthropic 약관이 전부 같은 문언을 쓴다 — 계정을 타인에게
제공하지 말 것, 한도를 우회하지 말 것. 즉 **AMX의 합법성은 프로바이더가 아니라
운영 규칙에서 갈린다.** 한 사람 소유 시트를 그 사람이 쓰는 서버들 사이에서
옮기는 것은 어느 약관에도 저촉되지 않지만, 여러 사람의 시트를 한 서버에서
돌리거나 소진 시 남의 시트로 자동 전환하는 것은 셋 모두 위반이다. 현행 Claude
풀 자동화는 소유자 구분 없이 서버 단위로 로테이션하므로 같은 위험에 노출돼
있다. 단속 공식 공지는 세 곳 모두 확인하지 못했으나, Codex 쪽에는 데이터센터
IP 대역 때문에 "headless proxy"로 오탐 정지된 Pro 구독자 사례가 있어 서버
배치형 운영 자체가 탐지 표적이 될 수 있다는 점은 감안해야 한다.

여기서 갈리는 결정이 하나 생긴다 — **Claude 풀 로테이션을 소유자 범위로
좁힐지**(조직 소유 시트는 조직을 소유자로 두어 풀 유지). 본 계획은 정책 축을
만들고 기본값을 소유자 범위로 두되, 최종 운영 방침은 사용자 결정 사항으로
남긴다.

## 2. 아키텍처

```
ams-server ── gRPC ── ama-agent
                        ├─ provider.Driver (claude / grok / codex)   ← 기존 인터페이스
                        ├─ Bridge 선택: AMX_SEAT_ENGINE=tsamx | native
                        │    ├─ tsamx Bridge (현행, 보존)
                        │    └─ native Bridge (신규)
                        │         ├─ ProfileStore   계정별 CONFIG_DIR 프로파일
                        │         ├─ Switcher       활성 포인터 원자 갱신
                        │         ├─ UsageCollector oauth/usage + 예산 정책 이식
                        │         ├─ AutoSwitch     threshold/cooldown/hysteresis + 격리
                        │         └─ PolicyGuard    소유자 바인딩·로테이션 범위
                        └─ 운영자 CLI: ama-agent seat {list,status,switch,…} --json
      런너 래퍼(amx-claude/amx-grok): 활성 포인터 → CLAUDE_CONFIG_DIR 주입
```

프로파일 디렉터리 방식의 핵심: 계정마다 `<state>/profiles/<provider>/<id>/`에
독립 config home을 두고, 전환은 `active` 포인터 파일을 바꾸는 것이다. 래퍼가
실행 시점에 포인터를 읽어 `CLAUDE_CONFIG_DIR`(Grok은 `GROK_HOME`)을 주입한다.
라이브 파일을 맞바꾸지 않으므로 foreign/alien/wiped 분류, Claude Code 자체
락과의 경합, 백업 슬롯 무결성 문제가 구조적으로 사라진다. 공통 설정
(settings.json, 훅, 스킬 경로)은 템플릿에서 프로파일 생성 시 복사하고 이후
변경은 동기화 명령으로 밀어 넣는다. 이미 떠 있는 세션은 다음 실행부터 바뀐다.

## 3. 단계

각 단계는 독립 PR 묶음이고, tsamx 브리지는 끝까지 손대지 않는다.

### P0 — 계약 고정과 버전 협상 (R1)
- 현행 tsamx 브리지의 동작을 골든 테스트로 고정한다: List/Status/Switch/Add/
  Remove/Enable/Disable/ConfigSet/AutoTick의 입력·출력·이벤트(exit 0/1/2/3 →
  KIND_SWITCH/ALL_EXHAUSTED)와 PoolSummary 계산. 새 엔진은 이 테스트를 그대로
  통과해야 한다.
- 에이전트가 `RegisterServer.tsamx_version`을 채우지 않는 공백을 메운다:
  엔진 종류·버전을 보고하고 서버가 콘솔에 표시한다. 새 엔진 배포 전에 필요하다.

### P1 — 정책 축 (R2, 서버+에이전트)
- `accounts.owner`(이미 nullable 존재)를 정책 입력으로 승격하고 테넌트 정책에
  `rotation_scope = owner | server`를 추가한다. 풀 권고 산출(pool.py)이 소유자
  범위를 존중하게 하되, 기본값은 `owner`로 둔다.
- 프로바이더별 능력 선언: `auto_switch: claude, grok` / `codex: 배포·회수만`.
  에이전트 스케줄러는 이 선언 밖의 프로바이더를 로테이션하지 않는다(현재 claude
  고정인 것을 선언 기반으로 일반화).

### P2 — 프로파일 스토어 (R3, 자격증명 취급)
- 프로파일 생성·삭제·스테이징(서버가 내려준 자격증명을 프로파일에 기록),
  지문(`sha256:hex(refreshToken)` 동치), 공통 설정 템플릿, 프로파일 단위
  파일 락. 기존 `provider.Driver`의 `StageCredential`/`Fingerprint`를 프로파일
  경로로 재구현한다.
- Claude·Grok 드라이버가 같은 스토어를 쓰고 Codex는 기존 단일 디렉터리 방식을
  유지한다(이동식 단일 보유 원칙).

### P3 — 전환기와 래퍼 (R3)
- `active` 포인터 원자 갱신, `switch`의 동기 완료 보장(반환 시 List가 새 active를
  돌려줌 — 계약 C10).
- amx-claude 래퍼가 포인터를 읽어 config dir을 주입한다. 포인터가 없으면 tsamx
  `status --json`으로 폴백(이행 기간 호환). Langfuse 훅도 같은 폴백 순서.
- 배달 락(`.amx-deliver.lock`) 규약은 그대로 둔다.

### P4 — 사용량 수집기 (R2)
- tsamx `poll_policy.py`의 관측 기반 예산 모델(60분 슬라이딩 ~28건, 평균 180초,
  급박 60초, 소진 600초, 429 후 AIMD)을 Go로 이식한다. 이 지식은 버리지 않는다.
- Claude: `api/oauth/usage`. Grok: 공식 한도 조회 수단이 확인되지 않아 1차는
  429 관측 기반, 후속 리서치 항목. Codex: 기존 rollout `token_count` 판독 유지.
- 유휴 프로파일의 토큰 갱신: Claude Code가 돌지 않는 프로파일은 refresh가
  일어나지 않으므로 만료 전 엔진이 갱신하거나 만료를 `relogin_required`로
  보고한다. tsamx의 wiped 방어에 해당하는 경계 케이스를 여기서 명시적으로
  다룬다.

### P5 — 자동 전환 엔진 (R2)
- threshold/cooldown/hysteresis, 격리(`relogin_required`)와 격리 상태 파일
  원자 쓰기(계약 C12), exit 0/1/2/3 의미의 이벤트 매핑, PolicyGuard 통과분만
  후보로 삼는 선택 전략(best / next-available).

### P6 — 에이전트 통합과 섀도 운전 (R2)
- `AMX_SEAT_ENGINE` 플래그로 브리지를 고르고, 섀도 모드에서는 두 브리지를 모두
  돌려 List/PoolSummary를 비교 로그로 남긴다(쓰기 동작은 선택된 쪽만).
- dev PC → 노트북 순으로 카나리. 불일치가 0이 될 때까지 기본값은 tsamx.

### P7 — 이행 (R1)
- e2e를 두 엔진으로 이중 실행. 설치 스크립트는 tsamx 설치를 그대로 유지한 채
  엔진 플래그만 추가. 문서(TSAMX-GUIDE·DEPLOYMENT-RUNNER)에 엔진 선택과 폴백
  절차를 적는다.

### P8 — Grok 드라이버 (R3)
- `GROK_HOME` 프로파일, `auth.json` 스테이징, device-auth 온보딩, 엔터프라이즈
  `force_login_team_uuid`와의 정합. 구독 티어의 CLI 한도 포함 여부는 1차 출처
  확인이 안 됐으므로 착수 전 실증(P0급 실험)을 둔다.

### P9 — 후보 확장 (계획만)
- Copilot CLI: 조직 시트 API 기반 회수·재배급을 별도 "시트 API형" 통합으로
  검토. Gemini Code Assist도 같은 범주(라이선스 assign/unassign)이며 자격증명
  방식은 쓰지 않는다.

## 4. 리스크와 대응

- 비공식 API(`oauth/usage`, Codex `wham/usage`) 스키마 변경: 수집기를 프로바이더
  드라이버 안에 격리하고 실패 시 `usage=null`(미측정)로 강등해 자동화가
  멈추지 않게 한다.
- 서버 배치 운영의 오탐 정지: 소유자 범위 정책과 동시 보유 금지로 행동 패턴을
  "한 사람의 기기 이동"에 가깝게 유지한다. 완전한 방어는 아니다.
- 프로파일당 설정 분기: 템플릿·동기화 명령으로 줄이되, 프로젝트별 상태
  (`projects`)는 프로파일마다 따로 쌓이는 것을 받아들인다.
- 디스크: 프로파일 수만큼 config home이 늘어난다. 트랜스크립트 보존 정책을
  프로파일 삭제와 연동한다.

## 5. 확정 결정 (2026-08-23)

1. **로테이션 범위 = `owner`.** 소유자 라벨이 빈 계정은 조직 공용으로 해석해
   전 서버에 배정 가능하고, 라벨이 있는 계정은 같은 소유자의 서버에만 간다.
   기존 데이터는 전부 빈 값이라 전환 당일 동작은 현행과 동일하며, 라벨을 붙이는
   만큼만 정책이 켜진다. 계정 폼에는 이미 소유자 입력이 있고(RegisterModal·
   EditAccountModal), **서버 등록·수정에 소유자 입력을 새로 추가**한다.
2. **Gemini·Grok은 착수하지 않는다.** P8·P9의 Grok·Gemini 항목은 계획 기록으로만
   남기고 실행 대상에서 제외한다.
3. **Codex는 배포·회수·가시성까지만.** 자동 전환 제외를 프로바이더 능력 선언으로
   못 박고(P1), 기존 CodexBridge를 그대로 유지한다.
4. **운영자 CLI 이름 = `amx seat`.** 기존 `amx` 래퍼의 하위 명령으로 붙이며
   `tsamx` 바이너리 이름은 쓰지 않는다.

실행 대상은 P0~P7이다.
