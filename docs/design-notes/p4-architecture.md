# AMX P4 "콘솔·운영" 아키텍처 설계서

> REASONER 산출(2026-08-08). 구현 브리핑의 근거. SSOT는 `docs/AMX-DESIGN.md` — 상충 시 SSOT 우선.
> **O8(ClickEye 연동)은 사용자 결정으로 P4에서 건너뜀** — 계정 스위칭 관제에 집중. Track D(ClickEye
> read-only API) 제외, O8은 미해결 유지.

## 핵심 설계 결정 7개

1. **BFF 강제(R3).** ams-web는 Next.js Route Handler를 통해서만 ams-server REST를 호출. 관리자 Bearer
   토큰(`AMX_ADMIN_TOKEN`)은 Next 서버 프로세스 env에만, **브라우저에 절대 노출 금지**. 브라우저↔BFF는
   httpOnly·SameSite=Strict 세션 쿠키. §7 "브라우저 토큰 노출 방지"의 유일한 구조적 집행점.
2. **실시간 = 폴링(SSE/WS 아님).** ams-server는 동기 FastAPI, gRPC는 별도 프로세스, 결합은 DB뿐. 브라우저가
   BFF 경유 5~10s 폴링(SWR/react-query refetchInterval)이 async 명령 수렴 + 5분 usage 케이던스에 정합. SSE는 확장점.
3. **경보는 신규 `alerts` 테이블 + REST.** P3가 훅으로 이월한 all_exhausted를 소비. 소스 = all_exhausted
   이벤트 + reconcile 드리프트 + 서버 오프라인. alembic 신규.
4. **all_exhausted 훅 = P4 소유(백엔드).** 현재 `_store_event`는 all_exhausted를 usage_snapshots 저장만.
   P4에서 alert 생성 + dedupe + auto-resolve로 승격.
5. **인증은 단일 관리자 유지 + 세션 계층만.** ams-server 단일 Bearer 그대로. 추가 = BFF 로그인→쿠키 세션 +
   `require_admin`을 `Principal` 반환형으로(P5 테넌트 스코핑 훅 자리). P4에서 멀티테넌트 RBAC 미구현.
6. **O8(ClickEye) = P4에서 건너뜀(사용자 결정).** Track D 제외, 미해결 유지.
7. **완료판정 검증 = BFF API 레벨 + 최소 Playwright.** "전 수명주기 조작"은 BFF Route Handler 프로그램
   구동으로 판정, OAuth 등록 마법사·deliver/recall만 실브라우저 스모크.

## 1. ams-web 스택·구조
- Next.js **App Router**, TypeScript, RSC 기본 + 상호작용 지점만 Client Component. 데이터 패칭은 서버
  컴포넌트에서 BFF 함수 직접 호출, 폴링·뮤테이션 뷰만 클라이언트 `/bff/*` fetch.
- `contracts/gen/typescript` 생성 타입을 `src/lib/api-client`가 소비(계약 우선). BFF가 이 타입으로 검증.
- 데이터 흐름: `Browser →(cookie)→ Next Route Handler(BFF) →(Bearer, server-side)→ ams-server REST →DB`.
  명령 액션은 202 수신, 수렴은 폴링 관측(즉시 반영 아님을 UI가 명시 — pendingCommandId/delivering 표시).

## 2. 모니터링 대시보드
- 서버 뷰: `GET /servers`(online/offline/degraded, lastSeenAt, agentVersion, assignedAccountCount).
  **degraded는 현재 미세팅** — P4에서 정의(반복 명령실패/부분 드리프트) 또는 enum 제거 결정.
- 풀·배정 뷰: `GET /assignments`(state 필터) + `GET /accounts`.
- 사용량·드리프트 뷰: `GET /servers/{sid}/usage`(poolSummary.maxUtilizationPct/allExhausted, drift[]).
- 이벤트 타임라인: switch/all_exhausted가 usage_snapshots(report_type="switch_event")에 있으나 조회 API
  부재 → **신규 `GET /servers/{sid}/events`**(또는 alerts에 흡수).

## 3. CRUD + 상태조작
- 테넌트/계정/서버/배정 CRUD는 openapi 그대로 프록시.
- OAuth 등록 마법사(§5.5): `:oauth-start`→authorizeUrl 표시→관리자 브라우저 로그인→코드 붙여넣기→
  `:oauth-complete`. flowId를 마법사 클라이언트 state로, 코드는 BFF 경유 1회 전송.
- 상태 전이 버튼: deliver/recall/activate/deactivate/recover/switch-now(assignment),
  switch-mode/refresh-usage/set-policy(server). 전이 규칙(§5.2)을 UI가 알아 불가 전이 버튼 비활성.
  정책(threshold/strategy) 편집 = PATCH server 컬럼 + 재하달.

## 4. 경보 (신규 백엔드 + UI)
**신규 모델 `alerts`** (alembic 0004):

| 컬럼 | 비고 |
|---|---|
| id, tenant_id, server_id?, account_id? | 테넌트 격리는 server 복합FK 재사용 |
| kind | all_exhausted \| drift \| server_offline \| quarantine |
| severity | critical / warning |
| status | open \| acked \| resolved |
| dedupe_key | (server_id, kind[, account]) — open 1건 유지 |
| detail (JSONB), source_snapshot_id? | |
| created_at, acked_at, acked_by, resolved_at | |

- **트리거 배선(전부 gRPC 프로세스/서비스 계층, 신규 스케줄러 없음):**
  - all_exhausted/quarantine → `_store_event`가 alert upsert(dedupe).
  - 드리프트 → `reconcile_from_report`가 drift 마킹과 동시에 alert upsert.
  - 서버 오프라인 → `_mark_offline` + **last_seen_at 스위퍼**(§9 위험).
- **auto-resolve:** 다음 UsageReport가 allExhausted=false거나 드리프트 소거 시 open→resolved.
- REST: `GET /alerts`(tenant/status/kind 필터), `POST /alerts/{id}:ack`. UI = 상단 배지 + 목록 + ack.
  외부 채널(Slack/이메일)은 **P4 범위 밖**(사용자 결정) — detail + resolved 훅을 확장점으로만.

## 5. 인증·RBAC
- ams-server: 단일 Bearer 유지. `require_admin` → `Principal`(현재 admin 단일) 반환으로 리팩터 = P5 훅 자리.
- BFF: 로그인 페이지 → 관리자 토큰/비밀번호 검증 → httpOnly 세션 쿠키. 이후 브라우저는 쿠키만, BFF가 Bearer 부착.
- **openapi/코드 불일치:** openapi "Tenant RBAC enforced"는 코드에 없음 → P4는 단일 관리자임을 §7·openapi에 명시.

## 6. 구현 순서·트랙 (ClickEye Track D 제외)
- **Track A(백엔드, R2/R3):** alerts 모델+alembic 0004 → `_store_event`/`reconcile`/offline 배선 →
  all_exhausted 훅 → `GET /alerts`·`:ack`·`GET /events` + last_seen_at 스위퍼. web 무관, 선행 가능.
- **Track B(BFF+인증, R3):** Next 스캐폴드, 로그인→쿠키, Route Handler 프록시, 생성 ts 클라이언트.
- **Track C(UI, R1):** 대시보드/CRUD/액션/OAuth 마법사/경보 패널. B 골격 후.
- 병렬: A ∥ B → C. **R3 지점:** (a) BFF 토큰·쿠키 취급, (b) gRPC 프로세스 내 alert 생성(reconcile 동일 트랜잭션 동시성).
- 마일스톤: M1 alerts 백엔드+all_exhausted 훅 → M2 BFF+로그인 왕복 → M3 대시보드+CRUD+액션 →
  M4 OAuth 마법사+경보 UI → M5 전수명주기 판정.

## 7. 테스트 전략
- 백엔드 alerts: 기존 **pytest** — 이벤트/드리프트/오프라인→alert 생성, dedupe, ack, auto-resolve.
- BFF: 생성 ts 클라이언트 타입 계약 + **API 레벨 통합**(Route Handler 프로그램 구동)으로 "전 수명주기 조작" 판정.
- 실브라우저: **Playwright 최소 스모크**만 — OAuth 등록 마법사, deliver/recall. 완료판정 게이트는 API 레벨이 SSOT.
- P2/P3 docker-compose E2E 하네스 재사용(가짜 AMA 세션 + 프리시드 usage)으로 라이브 백엔드 제공.

## 8. 미해결·위험
- **서버 오프라인 정합(신규 위험):** last_seen_at 만료돼도 gRPC 스트림 half-open이면 online 고착 → 오프라인
  경보 미발화. **스위퍼 필수**(주기 last_seen_at < now-3틱 → offline). Track A 포함.
- **degraded 미사용:** 정의 없이 enum만. P4에서 정의 또는 제거 결정.
- **경보 폭주:** dedupe_key + auto-resolve 없으면 5분마다 중복. open 단일화 필수.
- **P3 이월 유지:** Outbox best-effort 이벤트 유실 — 경보를 이벤트가 아닌 reconcile-on-report
  (usage_snapshots.drift) 재조회로도 재구성 가능하게 설계(이벤트 유실 시 자가치유).

## SSOT / openapi 수정 제안
1. **§5.6 경보**(신설, 이 브랜치에서 반영): alerts 테이블·트리거·auto-resolve·BFF 인증 명문화.
2. **§5.3 + openapi:** `GET/POST /alerts`, `:ack`, `GET /servers/{sid}/events` 추가.
3. **§7 + openapi securitySchemes:** BFF httpOnly 쿠키 세션 + 관리자 Bearer 서버측 전용 명시. "Tenant RBAC
   enforced" 문구를 P4 단일관리자 실상으로 수정(P5 훅 명시).
4. **§5.1 servers.status:** degraded 정의 또는 제거.
5. **O8:** P4 건너뜀·미해결 유지(반영 완료).
