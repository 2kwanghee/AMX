# Langfuse 모니터링 트랙 인수인계 (2026-08-15)

이 문서는 Langfuse 기반 모니터링 보강 작업(기획 → P1~P4)의 종료 시점 상태를 다음 세션에 넘기기 위한 것이다. 기획안 원문: https://claude.ai/code/artifact/d3776829-d97a-4d57-95c3-94114f48bb4e · 진행 메모리: `~/.claude/projects/-mnt-c-workspace-AMX/memory/amx-langfuse-plan.md` (러너 프로필 `~/.claude-amx`에도 심볼릭 링크로 연결됨).

## 완료된 것

| 단계 | 내용 | PR (전부 main 머지됨) |
|---|---|---|
| P1 | tsamx가 버리던 spend·모델별 윈도우를 계약→AMA→AMS payload까지 관통 | #69 |
| P1 | usage_snapshots 보존 정책(기본 90일, usage 행만, 정산 boundary 이전만, 미래 watermark 시 중단) | #70 |
| P1 | G27 watermark 미래 전진 경보(`billing_watermark_future`) | #71 |
| G27 | 근본 수정 — watermark 미래 주차 자가치유(되감기) + `snapshot_purge` 커서 클램프 | #72 |
| P3 | amx-claude 래퍼 경유 Stop 훅 중앙 배포(opt-in env 파일, tsamx 활성 이메일=userId, hostname=environment) | #73 |
| 후속 | 선재 테스트 7건 해소(P2a 상수→프로필 드리프트) — 이후 전체 스위트 0 failed 유지 | #74 |
| 후속 | Langfuse v4 운영용 compose(워커 기동 순서 healthcheck — 웹은 `$(hostname)` 바인딩이라 localhost 불가) | #75 |
| 후속 | fleet 일괄 on/off/status + agent-setup opt-in 자동 설치 | #76 |
| 후속 | 보존 삭제용 부분 인덱스(alembic 0020) | #77 |
| P4 | Langfuse Metrics API 주기 집계 스윕(7번째, 락 …07) + `langfuse_usage_rollup` + REST | #78 |
| P4 | 콘솔 usage 탭 LangfuseUsagePanel(모델별·계정별 실측, uiUrl 딥링크) | #79 |

P2(PoC)는 시험 장비 `~/langfuse-poc`에 Langfuse v4.11.0이 기동돼 있고(웹 포트 3100, org=amx / project=amx-poc, 키·admin 비밀번호는 `~/langfuse-poc/.env`), 이 PC의 러너 프로필(`~/.claude-amx`)에 훅이 설치돼 실 세션 추적이 검증됐다.

## 남은 작업

1. **P5 — Monitors 알림.** Langfuse Monitors(비용·지연 임계값 → 웹훅)와 AMX 자체 경보 외부 채널(p4-architecture.md:66의 확장점)을 하나의 알림 경로로 묶는 단계. 기획안 2.5절 참조. 착수 전 결정: 알림 채널(Slack/사내 메신저)과 임계값.
2. **운영 환경 반영.** 스윕·REST는 머지됐지만 운영 AMS에 `AMX_LANGFUSE_*` 설정(BASE_URL/PUBLIC_KEY/SECRET_KEY/TENANT_ID, 선택 UI_URL/POLL_SECONDS/WINDOW_DAYS/MAX_ACCOUNTS)이 없으면 비활성이다. alembic 0021 적용도 필요. 시험 장비 상시 기동 여부(ClickHouse 포함 스택)도 미결.
3. **파일럿 확대.** 훅은 현재 이 PC 한 대뿐. 노트북·운영 서버 확대 시 `deploy/fleet-langfuse.sh on` 사용(운영 호스트는 기본 `~/AMX`, dev 호스트는 `--remote-repo ~/AMX-agent`). 노트북 접근이 필요하면 10.60.1.15:3100 portproxy·방화벽 구성이 선행돼야 한다(WSL IP 변동 주의).
4. **amx-codex 미추적.** Langfuse 훅은 Claude Code 전용이라 codex 러너는 관측 공백. 멀티 프로바이더 계획(리서치 완료, 결정 3건 대기)에 종속.
5. **G27 후속(선택).** 되감기 자가치유로 핵심은 닫혔으나, 이미 billing 앵커가 생성된 날의 지각 스냅샷 반영은 G26 void/재집계 설계 소관으로 남아 있다.

## 결함 (미해결, 우선순위순)

- **[중] P4 캐시 토큰 항상 0** — Langfuse Metrics API에 캐시 토큰 measure가 없어 `cache_read/creation_tokens`를 0으로 적재한다. 웹 패널이 이 컬럼을 표시하므로 사용자가 "캐시 미사용"으로 오인할 수 있다. 웹에 각주를 달거나 컬럼을 숨기는 한 줄 수정 권장. 원 데이터는 trace 인제스천에는 존재하므로(usageDetails), Langfuse가 measure를 추가하면 마이그레이션 없이 백필 가능.
- **[하] 사라진 key의 stale 행 잔존** — P4 upsert는 이번 fetch에 있는 key만 갱신한다. observations가 append-only라 실무 위험은 낮다.
- **[하] Metrics API grouped 행 상한 미확인** — 모델 종수가 매우 많은 날 grouped 결과가 캡되면 일부 누락 가능(미검증 가설).

## 발견물 (기록 유지, 조치 불요 판단)

- 세션 도중 계정 전환 시 Langfuse userId가 기동 시점 계정으로 고정(DEPLOYMENT-RUNNER.md §8에 문서화됨). 비용 배분의 주 근거는 소진율이라 당장 실해 없음.
- 재설치 시 `.bak`에 구 시크릿 평문 잔존(문서화됨). 키 로테이션 시 .bak 정리 필요.
- 경보 resolve 경로가 평시 테넌트당 UPDATE 1회 발행 — kind 단일 UPDATE로 합치면 테넌트 수 무관(성능 개선 여지).
- `snapshot_purge` 커서의 배치-갭 TOCTOU — 유해 조건(최근 부분삭제 ∧ 동시 되감기)이 공존 불가로 판정돼 감시 대상으로만 기록.
- 웹 패널 Suggestion 3건: uiUrl href 스킴 미검증(관리자 설정값이라 저위험), 모델 행 클릭이 키보드 접근 불가(기존 패널과 동일 패턴), from>to 클라이언트 미검증.
- fleet `status`는 env 파일 존재만 확인 — settings.json 훅 등록·키 유효성은 안 본다(문서화됨).
- Langfuse v4는 구 traces API 미제공(events_only) — 연동은 반드시 `/api/public/v2/observations`·`/api/public/v2/metrics` 사용. 저장 payload는 ClickHouse `events_full` 테이블.
- compose 최초 기동 시 워커가 웹 마이그레이션보다 먼저 뜨는 문제는 #75의 healthcheck 의존으로 해결됐으나, 기존 PoC(`~/langfuse-poc`)는 구판 compose 그대로다 — 재기동 시 신판(deploy/langfuse/)으로 갈아타는 것을 권장.

## 다음 세션 시작 방법

`/mnt/c/workspace/AMX`에서 시작하면 메모리 인덱스(amx-langfuse-plan)가 자동 로드된다. P5를 진행하려면 기획안 2.5절과 이 문서의 "남은 작업 1"부터. 운영 반영을 먼저 하려면 "남은 작업 2"의 설정 목록과 `deploy/langfuse/README.md`(스택 기동), `docs/DEPLOYMENT-RUNNER.md` §8(훅 설치)을 참조.
