# 대시보드 메뉴 개편 계획 · 집계 통계 전용 화면

작성일 2026-08-22 · 상태: 기획(결정 대기)

## 0. 한 줄 요약

대시보드 탭에서 조작 기능을 전부 걷어내고, 서버별·계정별 집계 통계를 애니메이션이
들어간 SVG 위젯 8종으로 보여주는 화면으로 바꾼다. 차트 라이브러리는 들이지 않고
손수 SVG + CSS/rAF 로 그리되 계산만 d3 서브패키지를 빌린다. 단, 사용자가 예로 든
통계 중 두 가지(서버별 호출 횟수·최다 모델, 계정이 쓰인 업무)는 지금 수집되는
데이터에 서버/프로젝트 축이 없어서 **수집 스키마 보강(PR 0)이 선행**돼야 한다.

## 1. 왜 바꾸나: 현재 대시보드의 실제 구성

`ams-web/src/app/dashboard/page.tsx` 기준. 10탭 사이드바, 2번째가 상황판.

| 홈 탭 구성 요소 | 성격 | 조작 기능 | 같은 기능이 있는 곳 |
|---|---|---|---|
| KpiStrip (page.tsx:190) | 통계 | 카드 클릭 → 탭 이동 |: |
| ServersPanel variant=home | 혼합 | 등록·전환모드·갱신·모달·토큰·업데이트·삭제 | 서버 탭, 상황판 팝오버 |
| AssignmentsPanel | 혼합 | deliver/activate/recall… verb 버튼, 신규 할당 | 할당 탭, 상황판 엣지 |
| ActivityFeed (page.tsx:252) | 표시 전용 | 없음 |: |

통계 성격 패널 3종(UsageCost·Langfuse·Session)은 전부 `<table>` + 숫자다. SVG
그래픽은 KPI 스파크라인 하나뿐. 즉 "통계는 표로만, 조작은 중복으로" 들어 있는
상태라 사용자 판단(대시보드 ≠ 콘솔)이 코드 구조와도 맞는다.

## 2. 지금 수집하는 데이터 (코드 본문 기준, `ams-server/app/models.py`)

| 데이터 | 시간축 · 해상도 | 서버 축 | 계정 축 | 모델 축 | 보존 |
|---|---|---|---|---|---|
| `usage_daily_rollup` 사용률 적분(held/observed 초) | 일 | ○ | ○ | ✕ | 무제한 |
| `langfuse_usage_rollup` 토큰 5종·observation | 일 | ✕ | ○(이메일 문자열) | ○ | 무제한 |
| `session_usage` 토큰 8종·메시지수·stop_reason·tier | 세션 | ✕ | △(nullable) | ○ | 90일 |
| `usage_snapshots` 보고 원문(창 %·spend·scoped_windows) | 5분 | ○ | ○ | △(잔여%만) | 90일 |
| `account_usage_windows` 5h/7d 잔여 % | 현재값만 | ○ | ○ | ✕ |: |
| `servers` cpu/mem/disk·상태·버전 | 현재값만 | ○ |: |: |: |
| `alerts` 16종 | 이벤트 | ○ | ○ |: | 무제한 |
| `pool_events`·`pool_chains` | 이벤트 | ○ | ○ |: | 90일 |
| `assignments`·`agent_commands` | 이벤트(sent→acked 지연) | ○ | ○ |: | 90일 |
| `usage/cost` 월별 서버→계정 비용 배분 | 월 | ○ | ○ |: |: |
| `admin_audit_logs` | 이벤트 |: |: |: | 무제한 |

집계 GROUP BY 를 받는 API는 없다. 그룹 축이 전부 하드코딩돼 있어 대시보드용
집계 엔드포인트를 새로 만드는 편이 낫다.

## 3. 요청한 통계가 지금 되는가

| 요청 통계 | 판정 | 근거 |
|---|---|---|
| 서버별 사용량(시간 점유) | 즉시 가능 | `usage_daily_rollup` |
| 서버별 비용 | 즉시 가능 | `/usage/cost` |
| 서버별 최다 사용 계정 | 즉시 가능 | rollup GROUP BY server,account |
| 서버별 전환 횟수·명령 성공률 | 조인 | `usage_snapshots(switch_event)`, `agent_commands` |
| **서버별 호출 횟수** | **수집 없음** | `message_count`·`observation_count` 둘 다 server_id 없음 |
| **서버별 최다 모델** | **수집 없음** | 모델 축 테이블에 서버 축 없음 |
| 계정별 총 사용량·순위 | 즉시 가능 | rollup 또는 langfuse user 차원 |
| 계정별 토큰·모델 구성 | 조인 | langfuse `key(email)`↔`accounts.email` (provider 구분 없음) |
| 계정별 세션 수·메시지 수 | 조인 | `session_usage.account_id` |
| **계정이 쓰인 업무(프로젝트)** | **수집 없음** | 훅이 `hostname`·`cwd`를 보내지만 `record_session_usage`가 버림 (`app/services/session_usage.py:120-171`) |
| 계정 잔여량 추이 | 재구성 필요 | windows는 upsert, 이력은 snapshots payload에서만 |

### 결정 1: 수집 보강 범위 (사용자 선택)

- (a) `session_usage`에 `server_id`(hostname→servers 매핑)·`project`(cwd 말단) 컬럼
  추가 + 마이그레이션 + ingest 저장. → 서버별 호출 횟수·최다 모델·계정별 업무 전부
  해결. 훅 페이로드는 이미 보내고 있어 에이전트 수정 없음. **권장.**
- (b) 보강 없이 지금 데이터로만. 서버별 통계는 "시간 점유·비용·계정"만, 모델 통계는
  테넌트 전역으로만 보여준다.

### 결정 2: "사용량"의 정의

rollup은 시간 점유 적분이고 langfuse/session은 토큰이다. 서버 축 통계는 전자,
모델·계정 축 통계는 후자로 **위젯마다 단위를 명시**하는 쪽을 권장한다. 하나로
통일하려면 (a)가 전제.

## 4. 화면 설계: 위젯 8종

상황판이 노드·엣지 언어이므로 대시보드는 **면(area)·호(arc)·격자(grid)** 언어로
구분한다. 조작 요소는 기간 선택(24h/7d/30d) 하나만 남기고, 클릭은 전부
"해당 탭으로 이동"에 한정한다.

```
┌─────────────────────────────────────────────────────────────┐
│ 헤더: LIVE 펄스 · 갱신시각 · 기간 [24h|7d|30d]              │
├──────────┬──────────┬──────────┬──────────┐                 │
│ KPI 토큰  │ KPI 비용  │ KPI 세션  │ KPI 경보  │ ← ① 카운트업  │
├──────────┴──────────┴──────────┴──────────┴─────────────────┤
│ ② 시계열 누적영역 (토큰 by 모델 / 점유 by 서버)              │
├───────────────────────────┬─────────────────────────────────┤
│ ③ 서버→계정 흐름(Sankey)   │ ④ 모델 점유 도넛 + 순위          │
├───────────────────────────┼─────────────────────────────────┤
│ ⑤ 계정 순위 바 (기간 변경 시 재정렬) │ ⑥ 계정 잔여 원형게이지 격자 │
├───────────────────────────┼─────────────────────────────────┤
│ ⑦ 요일×시간 히트맵(세션)   │ ⑧ 이벤트 타임라인(경보·전환·풀)   │
└───────────────────────────┴─────────────────────────────────┘
```

| # | 위젯 | 데이터 소스(신규 API) | 구현 | 애니메이션 |
|---|---|---|---|---|
| ① | KPI 타일 4 + 스파크라인 | `/stats/summary` | rAF 숫자 보간, 기존 `Sparkline` 확장 | 마운트 카운트업, 갱신 시 이전값→새값 보간, 증감 화살표 |
| ② | 누적 영역 시계열 | `/stats/timeseries?by=model\|server` | d3-shape area + d3-scale | 마운트 path 드로우, 갱신 시 `d` 보간, 호버 크로스헤어 |
| ③ | 서버→계정 Sankey | `/stats/flows` (rollup) | d3-shape linkHorizontal | 링크 두께 grow, 호버 경로 하이라이트 |
| ④ | 모델 점유 도넛 | langfuse model 차원 (결정1-a면 서버 필터) | SVG arc stroke-dasharray | 0→비율 진행, 세그먼트 호버 팽창 |
| ⑤ | 계정 순위 바 | `/stats/accounts` | absolute 바 + FLIP transform | 기간 변경 시 재정렬 이동 |
| ⑥ | 잔여 게이지 격자 | 기존 `accounts.usage`(PR #147) | SVG 원호, 0~100 클램프 | 채움 진행, 70/90 임계 색 전이(기존 tone 규칙 재사용) |
| ⑦ | 요일×시간 히트맵 | session_usage `ended_at` | CSS grid + 색 보간 | 셀 stagger 진입(30ms, 상한 300ms) |
| ⑧ | 이벤트 타임라인 | alerts·switch_event·pool_events 병합 | 기존 ActivityFeed 확장 | 새 항목 슬라이드인 + 도착 하이라이트(PR #138 규칙) |

⑥은 PR #147로 이미 있는 데이터를 쓰고, ⑧은 ActivityFeed를 거의 그대로 살린다.
나머지 6개가 신규.

## 5. 기술 스택

| 항목 | 선택 | 이유 |
|---|---|---|
| 렌더 | 인라인 SVG + CSS + rAF | CSP `default-src 'self'`로 외부 리소스 불가, 기존 코드 전부 손수 SVG, canvas 없음 |
| 계산 | `d3-shape`·`d3-scale`·`d3-interpolate`·`d3-sankey` 서브패키지만 | gzip 30KB 미만, 순수 계산이라 SSR 무관. `d3` 메타패키지 금지 |
| 모션 | 기존 토큰 `--m-fast/base/move`, `--ease-out/inout` + `prefers-reduced-motion` 8곳 규칙 동일 적용 | 상황판·풀 패널과 같은 언어 |
| 대안(기각) | Recharts 3 | +100KB, Sankey·바 레이스·게이지는 어차피 손수, 상황판과 룩 겹침 |

검증 환경 제약: `npm run dev` 는 CSP 때문에 빈 화면 → `next build && next start`.
DOM 테스트 환경 없음(vitest node). 위젯 테스트는 **계산 함수(스케일·경로·집계)
순수 함수 분리 + 스냅샷** 으로 한다. Hydration: 초기 렌더는 최종값, 애니메이션은
`useEffect`에서만 시작(`useNow` 패턴, `common.tsx:466`).

## 6. 구현 단계 (PR 단위)

| PR | 내용 | 소유 파일 | 리스크 |
|---|---|---|---|
| 0 | (결정1-a 시) `session_usage`에 `server_id`·`project` 추가, ingest 저장, hostname→server 매핑 | `ams-server/app/models.py`, `alembic/versions/0032_*`, `app/services/session_usage.py`, `app/api/v1/ingest.py` | R2(마이그레이션) |
| 1 | 집계 API 4종 `GET /tenants/{t}/stats/{summary,timeseries,flows,accounts}` + 기간 파라미터 + BFF allowlist + 클라이언트 타입 | `app/api/v1/stats.py`, `app/services/stats.py`, `ams-web/src/lib/server/upstream.ts`, `api-client/{client,types}.ts` | R2(공개 API) |
| 2 | 차트 프리미티브: `useCountUp`, `useInterpolatedPath`, `Donut`, `Area`, `Gauge`, `Heatmap`, `Sankey` + d3 서브패키지 도입 + 순수 함수 테스트 | `ams-web/src/components/charts/*`, `package.json` | R1 |
| 3 | 홈 탭 교체: ServersPanel(home)·AssignmentsPanel 제거, 위젯 ①②④⑥ 배치, 기간 선택 | `page.tsx`, `src/components/dashboard/*`, `globals.css` | R1 |
| 4 | 위젯 ③⑤⑦⑧ 추가, 순위 FLIP, 타임라인 병합 | 동일 | R1 |
| 5 | 마무리: reduced-motion 점검, 빈 데이터(Langfuse 미설정) 상태, 번들 분석, 문서 |: | R0 |

PR 2~4는 PR 1의 응답 타입만 고정되면 병렬 가능(msw 핸들러로 모킹). PR 0이 없으면
PR 1의 timeseries `by=server`는 rollup(시간 점유)으로만, 도넛의 서버 필터는 뺀다.

## 7. 삭제되는 것 / 유지되는 것

- 삭제: 홈 탭의 ServersPanel(variant=home)·AssignmentsPanel. 다른 탭의 동일
  컴포넌트는 그대로.
- 유지: KpiStrip 클릭 이동(통계→상세 탭 이동은 대시보드 성격에 맞음), ActivityFeed
  (⑧로 흡수), `useSeries`·`ConsoleHeader`·`LiveDot`·`markDataArrived`.
- 건드리지 않음: 상황판, 서버/계정/할당/풀/알림/사용량/감사 탭, SWR 키 규약(같은
  키 재사용으로 폴링 중복 금지: page.tsx:186 주석).

## 8. 미해결·발견물

1. (결정) 결정 1·2: 위 §3.
2. (결정) 기간 선택이 SWR 키에 들어가면 폴링이 기간별로 늘어남. 통계 API는 45s
   폴링 + `keepPreviousData` 로 두고 기간 변경 시만 재요청하는 안을 권장.
3. (발견물) `langfuse_usage_rollup.key`가 이메일 문자열이라 claude/codex 동일
   이메일 계정이 합쳐진다: 멀티 프로바이더 확장 전 provider 축 필요.
4. (발견물) `AMX_LANGFUSE_TENANT_ID`·`AMX_SESSION_TENANT` 단일 고정: 멀티테넌트면
   모델·세션 축은 한 테넌트만 채워진다.
5. (발견물) `servers` cpu/mem/disk는 현재값뿐이라 자원 추이 위젯은 이력 테이블
   없이는 불가. 이번 범위에서 제외.
6. (발견물) `next lint` 스크립트는 있으나 eslint 미설치: typecheck가 유일 게이트.
7. (확인 필요) Recharts 기각 근거 중 번들 수치는 근사치. 도입 안 하므로 영향 없음.

## 부록 A. 집계 API 계약 (PR1) · 확정 2026-08-22

공통: `GET /api/v1/tenants/{tenant_id}/stats/*`, `TenantScope` 의존성, 쿼리 `range=24h|7d|30d`(기본 7d).
`range` 경계는 UTC `now - range` 이상. 응답에 `range`, `as_of`(ISO) 공통 포함. 서버 축 "사용량"은
`usage_daily_rollup.held_util_seconds`(시간 점유, 단위 `seconds`), 토큰 축은 `session_usage`(단위 `tokens`).
24h 요청 시 rollup은 일 단위라 당일·전일 2행을 그대로 쓴다(시간 해상도는 토큰 축만).

| 경로 | 응답 |
|---|---|
| `stats/summary` | `{range, as_of, tokens:{value, prev}, cost:{value, currency, prev}, sessions:{value, prev}, alerts_opened:{value, prev}, alerts_open_now, servers_online, accounts_active, sparkline:{tokens:number[], sessions:number[]}}` · `prev`는 직전 같은 길이 구간 값. `alerts_opened`는 구간에 생성된 경보 수(현재 상태 무관), `alerts_open_now`는 지금 열린 건수. sparkline은 range를 12구간으로 나눈 토큰·세션 합 |
| `stats/timeseries?by=model\|server\|account` | `{range, as_of, unit:"tokens"\|"seconds", buckets:string[], series:[{key, label, values:number[]}]}` · `by=server`는 rollup(seconds), `by=model`·`account`는 session_usage(tokens). 버킷은 24h=1h, 7d=6h(rollup은 1d), 30d=1d. 시리즈는 합계 상위 8개 + `other` |
| `stats/flows` | `{range, as_of, unit:"seconds", nodes:[{id, kind:"server"\|"account", label}], links:[{source, target, value}]}` · rollup GROUP BY server,account |
| `stats/accounts` | `{range, as_of, rows:[{account_id, email, provider, tokens, sessions, messages, top_model, top_server_id, top_server_name, top_project, held_seconds, remaining_5h_pct, remaining_7d_pct}]}` · 토큰 내림차순, 상위 50 |
| `stats/servers` | `{range, as_of, rows:[{server_id(null=미귀속), name, status(+"deleted"), held_seconds, tokens, sessions, messages, top_model, top_account_id, top_account_email, cost:{amount,currency}}]}` · held_seconds 내림차순 |
| `stats/heatmap` | `{range, as_of, cells:[[7][24] sessions 수]}` · `session_usage.ended_at` UTC 요일×시 |

프론트 BFF allowlist(`ams-web/src/lib/server/upstream.ts`)에 `^tenants/${ID}/stats/(summary|timeseries|flows|accounts|servers|heatmap)$` 추가.
`cost`는 `usage_cost.compute_month_cost` 당월 합계를 그대로 쓴다(기간과 무관, 월 단위임을 UI에 표기).

### 부록 A 한계 (리뷰 반영 2026-08-22)
- `session_usage`는 세션×모델당 시작·종료 한 쌍만 있어 timeseries의 1h/6h 버킷은 토큰 전량을 **종료 시각 버킷**에 넣는다. 긴 세션은 한 버킷에 몰린다. UI 범례에 "세션 종료 시각 기준" 표기.
- `servers.hostname`은 이제 에이전트 Register 보고값으로 덮어쓴다(`app/grpc/server.py:_touch_server`). 관리자 수동 입력은 첫 접속 전까지만 유효.
- 삭제된 서버의 session_usage 행은 남는다. `stats/servers`는 기간 내 session_usage·rollup에 등장한 server_id를 모두 포함하고, servers에 없으면 `name="(삭제된 서버)", status="deleted"`, server_id NULL 세션은 `"(미귀속)"` 행으로 합산. flows·timeseries·accounts.top_server_name의 라벨 폴백도 동일.

### 발견물 (리뷰 A·B, 미처리)
- cwd가 홈 디렉토리면 `project`에 로컬 사용자명이 들어간다. 서버에서는 구분 불가 → 훅에서 `cwd == $HOME`이면 cwd를 비워 보내는 후속 PR 필요(훅 파일은 이번 범위 밖).
- 같은 hostname 서버가 둘 이상이면 재보고마다 `server_id`가 다른 서버로 바뀔 수 있다. hostname 동기화 후에는 드문 케이스라 미처리.
- `ix_session_usage_tenant_server`는 부록 A 쿼리(ended_at 범위 선행)에 효용이 낮다. 해가 없어 유지.
- `func.lower(servers.hostname)` 함수 인덱스 없음. 테넌트당 서버 수가 적어 미처리.
