# 계정 풀 자동 배분(배급처·충전소) 초기 기획

작성 2026-08-21 · 상태: 구현 완료(PR #132 #133 #134, 2026-08-21 main 머지) · 근거: 4개 조사 트랙(서버 배정 / 에이전트 / 사용량 신호 / UI·문서) · 현행 사양은 AMX-DESIGN.md §5.8

## 0. 한 줄 요약

테넌트·서버는 고정하고, 계정만 "배급처(가용 풀) → 대여중(서버 귀속) → 충전소(리밋 쿨다운) → 배급처" 순환을 돌게 하는 서버 측 컨트롤러를 만든다. 재료는 대부분 이미 있다 — 5시간/7일 창의 `pct`·`resets_at`이 5분마다 수집되고 있고(`usage_snapshots.payload`), deliver/recall/switch_now 명령 경로도 있다. 빠진 것은 **계정 단위로 그 값을 읽는 테이블**, **그 값으로 판단하는 루프**, **그 판단을 운영자가 보는 화면** 세 가지다.

## 1. 현재 상태 — 코드로 확인된 사실

| 항목 | 사실 | 근거 |
|---|---|---|
| 배정 제약 | 계정 1개 = 라이브(비-detached) 배정 최대 1개. 서버당 계정 수 제한은 없음(Codex만 1개) | `models.py:369-376` partial unique, `inventory.py:888-927` |
| 배정 전달 | REST → `agent_commands` 큐 → gRPC 0.5s 폴링으로 서명 푸시 → ack로 수렴 | `grpc/server.py:398-425`, `commands.py:440` |
| deliver의 의미 | 풀에 추가 + enable. **활성 전환은 하지 않는다** — 이전 활성으로 되돌림 | `handlers.go:184-198` |
| 활성 전환 | `switch_now` 명령 또는 에이전트 auto 틱(60s, 사용률 높으면 15~30s) | `scheduler.go:150-165` |
| 회수 | 항상 로컬 사본 purge. 재배정 = 자격증명 재전송 전체 | `commands.py:134-155` |
| 리밋 신호 | tsamx가 `api/oauth/usage` 폴링 → `windows[]{id,pct,resets_at}` 계정별로 서버 도착. JSONB에만 저장, `resets_at`은 아무도 안 읽음 | `reporter.go:192-207`, `usage_cost.py:109-134` |
| 서버가 해석하는 소진 | 서버 범위 `all_exhausted` bool 하나 (전원 pct≥95) | `alerts.py:317-330` |
| 계정 status | available/assigned/disabled/quarantined. disabled·quarantined는 자동 설정 경로 없음 | `models.py:39`, `inventory.py:587` |
| 백그라운드 | 30초 단일 스위퍼에 11종 형제 스윕, advisory lock | `grpc/server.py:1406-1583` |
| 감사 | REST 미들웨어만. 스윕·reconcile의 자동 변경은 감사 기록 없음 | `api/audit.py:107-120` |
| UI 불변식 | "활성" = state=active 중 `lastSwitchedAt` 최신. 수동 create → deliver 두 번 클릭 | `assignment-active.ts:11-30` |
| 문서 | "1인 1계정" 문구는 없음. 실체는 DB 유니크뿐. 자동 회수 정책 없음 | web-docs 트랙 |

## 2. 목표 상태

### 2.1 계정 풀 상태 머신 (신규, `accounts.pool_state`)

```
            ┌──────────────┐  배정 결정   ┌──────────────┐
  등록 ───▶ │  배급처       │ ──────────▶ │  대여중       │
            │  READY       │ ◀────────── │  LEASED      │
            └──────┬───────┘  회수 완료    └──────┬───────┘
                   ▲  resets_at 경과                │ 창 소진(pct≥임계) or 만료
                   │                                ▼
            ┌──────┴───────┐              ┌──────────────┐
            │  충전소       │ ◀─────────── │  회수중       │
            │  COOLING     │              │  RECALLING   │
            │  (5h / 7d)   │              └──────────────┘
            └──────────────┘
   별도: PINNED(수동 고정, 자동화 제외) · HELD(격리/사용불가, 운영자 개입)
```

- 충전소 분류는 별도 테이블이 아니라 **어느 창이 막혔는가**로 결정한다. `five_hour` 창이 ≥임계면 5시간 충전소, `seven_day`면 7일 충전소. 둘 다면 늦은 `resets_at`을 따른다. `cooling_until = max(resets_at of exhausted windows)`.
- 배급처 복귀는 `cooling_until` 경과 **그리고** 다음 usage 보고에서 pct가 복귀 임계(예: 20%) 이하로 관측됐을 때. 시각만 믿고 되돌리면 리셋 직후 한 번 더 소진되는 경우를 못 막는다.
- 기존 `assignments.state`는 건드리지 않는다. 풀 상태는 계정의 속성이고, 배정 상태는 전달 수렴의 속성이다.

### 2.2 서버 슬롯 정책 (신규, `servers.pool_policy`)

| 필드 | 의미 | 기본 |
|---|---|---|
| `mode` | `manual` / `auto` | manual (기존 동작 유지) |
| `target_leases` | 서버가 유지할 대여 계정 수 | 1 |
| `swap_at_pct` | 이 사용률 이상이면 교체 시작 | 85 |
| `prefetch_at_pct` | 이 사용률 이상이면 다음 계정을 미리 deliver(핸드오프용) | 70 |
| `min_lease_minutes` | 플래핑 방지 최소 대여 시간 | 30 |

"서버당 1계정 귀속"은 `mode=manual` 또는 `auto + target_leases=1 + pinned` 로 그대로 표현된다. 별도 기능이 아니다.

### 2.3 교체 시나리오 (10서버·20계정)

1. 서버 S1의 대여 계정 A1이 `prefetch_at_pct`에 도달 → 컨트롤러가 배급처에서 A11 선택, `create_assignment + deliver` (풀에 추가만 되고 A1이 계속 활성).
2. A1이 `swap_at_pct` 도달 → `switch_now(A11)`. 에이전트는 tsamx 락을 잡고 원자적으로 전환(기존 경로).
3. A1 → `recall`. detached 수렴 확인 후 A1 `pool_state=COOLING`, `cooling_until=resets_at`.
4. `cooling_until` 경과 + 복귀 관측 → A1 `READY`. 다시 후보.

핸드오프(1→2)가 있어야 서버가 무자격 상태로 떨어지는 공백이 없다. deliver가 활성을 바꾸지 않는 현재 동작이 여기서는 오히려 유리하다.

### 2.4 후보 선택 규칙 (배급처에서 뽑을 때)

제외: `PINNED`·`HELD`·`assignment_excluded`·라이브 배정 보유·Codex 서버 1개 초과·최근 `credential_unusable` 경보.
정렬: ① 7일 창 잔여 많은 순 ② 5시간 창 잔여 많은 순 ③ 마지막 대여 종료가 오래된 순(공평 순환). 동점은 account_id로 결정적으로.
타이브레이크를 결정적으로 두는 이유는 스윕이 30초마다 돌고 advisory lock 아래서 재진입하기 때문이다.

## 3. 구현 단계 — 작은 것부터

| 단계 | 내용 | 리스크 | 선행 |
|---|---|---|---|
| P0 정규화 | `account_usage_windows(tenant, account, window_id, pct, resets_at, usage_fetched_at, reported_at, server)` 테이블 + UsageReport 수신 시 upsert. 기존 JSONB는 유지. | R1 | 없음 |
| P0' 에이전트 결함 4건 | reporter가 `usage_fetched_at` 안 채움 / usage null 행이 pct=0·eligible로 집계 / `usageStatus=="quarantined"` 비교가 항상 false / Heartbeat active_account 미설정. **P0 테이블의 입력 품질이 이것에 달려 있다.** | R1 | 없음 |
| P1 관측만 | `pool_state`·`cooling_until` 컬럼 + 30초 스윕에서 **계산만 하고 명령은 안 보냄**. UI에 배급처/대여중/충전소 3열 + 타이머. 계정별 "80% 도달" 경보 추가. | R1 | P0 |
| P2 반자동 | 컨트롤러가 "교체 권고"를 만들고 운영자가 한 번 클릭으로 deliver→switch→recall 체인 실행(이미 TopologyView의 move 체인이 비슷한 걸 한다). | R2 | P1 |
| P3 자동 | `mode=auto` 서버에서 체인을 스윕이 직접 실행. 자동 변경 전용 감사 행(actor=`pool-controller`) + 자동화 일시정지 스위치(테넌트 단위) 필수. | R2~R3 | P2, 2주 이상 P2 운영 |

P1에서 멈춰도 가치가 있다. 지금은 운영자가 "누가 언제 풀리는지"를 아예 볼 수 없기 때문이다.

### 구현 현황 (2026-08-21)

P0~P3 전 단계가 브랜치에 올라왔다. P0/P0' `feat/pool-p0-usage-windows`, P1 `feat/pool-server-p1`, P2·P3 `feat/pool-server-p3`(권고·체인·자동 실행·일시정지), 웹 화면 `feat/pool-web`. 컨트롤러 동작은 환경변수로 조율한다. `AMX_POOL_WINDOW_HIGH_PCT=80`(계정 단위 고사용 경보 임계), `AMX_POOL_OBSERVATION_GRACE_MINUTES=15`(충전소 복귀 관측 유예), `AMX_POOL_CHAIN_STEP_TIMEOUT_MINUTES=10`(체인 한 단계 시간 초과), `AMX_POOL_MAX_CONCURRENT_CHAINS=3`(테넌트 동시 체인 상한), `AMX_POOL_WINDOW_STALE_MINUTES=30`(관측이 낡았다고 보는 경계), `AMX_POOL_EVENT_RETENTION_DAYS=90`(pool_events 보존). 값은 서버 배포 환경에서 덮어쓴다.

## 4. 기존 코드와 부딪히는 지점

1. **unique 제약** — 회수가 detached로 수렴하기 전에는 같은 계정을 다시 INSERT 못 한다. 컨트롤러는 `RECALLING` 계정을 후보에서 빼고, 수렴은 reconcile에 맡긴다.
2. **reconcile CORRECTION_CAP=3** — 컨트롤러의 deliver와 reconcile 재전달이 같은 assignment를 건드리면 in-flight 스킵. 컨트롤러는 `pending/delivering/recalling` 상태 배정이 있는 서버에는 새 명령을 내지 않는다(서버당 1 in-flight).
3. **새 command_type 추가 금지** — 구버전 에이전트가 unknown oneof를 REJECTED한다. deliver/recall/switch_now 3종 조합만 쓴다.
4. **recall은 purge** — 재대여마다 자격증명 전체 재전송. 트래픽보다 문제는 에이전트의 DeliverLock fail-open 구간(로그 없음)이다. P3 전에 fail-open을 이벤트로 남기게 해야 한다.
5. **감사 공백** — 자동 변경은 REST를 안 타서 감사가 없다. `pool_events` 테이블(누가·왜·어느 pct에서·어느 창 기준)을 P1부터 같이 넣는다.
6. **서버별 cooldown(PolicyModal)이 이미 있다** — 에이전트 auto 틱의 쿨다운과 풀 `min_lease_minutes`가 축이 겹친다. 규칙: 서버 정책은 "언제 전환하나", 풀 정책은 "무엇을 주고 언제 거두나". 전환 자체는 계속 에이전트 몫.
7. **API 키 계정(BACKLOG A3)** — 창 개념이 없으니 `PINNED`로 두고 자동화 밖에 둔다.

## 5. 결정이 필요한 것

1. 충전소 복귀 조건 — 시각만(단순) vs 시각+관측(안전). 제안: 관측 포함. **확정:** 시각+관측. `cooling_until` 경과 그리고 복귀 임계 이하 관측을 함께 요구하되, 관측이 안 오면 유예(기본 15분) 뒤 해제(`services/pool.py:707-713`).
2. 핸드오프 방식 — 미리 deliver해 두는 방식(서버에 계정 2개가 잠시 공존) vs 회수 후 전달(공백 발생). 제안: 미리 deliver. 단 Codex는 1개 제한이라 회수 후 전달로 분기. **확정:** 미리 deliver 핸드오프, Codex는 회수 선행으로 분기.
3. 자동화 범위의 첫 대상 — 테넌트 전체 vs 서버 단위 opt-in. 제안: 서버 단위 `mode=auto`. **확정:** 서버 단위 `mode=auto`(`servers.pool_policy.mode`, 기본 manual).
4. `swap_at_pct` 기본값과 에이전트 `SwitchThresholdPct`(95, reporter.go:33)의 관계 — 서버가 더 낮은 값에서 먼저 움직이게 할지. **확정:** swap_at_pct(85) < 에이전트 임계(95). 서버가 더 낮은 값에서 먼저 움직인다.

## 6. 발견물 (이번 목표와 무관, 별도 처리)

- `ServersPanel.tsx:729-761` 사용량 모달이 camelCase(`poolSummary`, `a.usage.windows`)를 읽는데 저장은 snake_case(`pool_summary`, `windows` 직속) — 모달이 비어 보일 가능성 높음 (확신도 상).
- `AMX-DESIGN.md:369` "계정 단위 제외는 pinned"는 stale. 실제는 `assignment_excluded`, `pinned`는 UI에 없음.
- `:recall {"force":true}` UI 버튼 없음 — curl로만 가능.
- `usage_daily_rollup.held_util_seconds`의 utilization은 CPU가 아니라 계정 창 pct. metrics.go의 CPU/MEM과 이름 충돌.
- `TopologyView.tsx:928-935` move 체인 중간 실패 시 계정이 어느 서버에도 없는 상태로 남음.
- `usage_snapshots.account_id` 컬럼이 항상 NULL(`grpc/server.py:838`).
