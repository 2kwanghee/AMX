# AMX — Account Management & eXchange 설계 문서

---
title: AMX 최종 설계 문서 (v1.1 — 정합성 교차 리뷰 반영 + tsamx 내재화 반영)
status: reviewed-baseline
last_updated: 2026-08-07
author: 기획설계 세션 (tsamx 분석 + AMS/AMA 설계 에이전트 종합, 독립 리뷰어 교차 검증)
---

## 1. 개요

AMX는 Claude 구독 계정(어카운트)의 **중앙 관제 + 자동 스위칭**을 제공하는 독립 프로젝트다.
내재화한 CLI 도구 `tsamx`(claude-swap 포크, `docs/TSAMX-GUIDE.md`)가 제공하는 단일 서버 계정 자동 스위칭을,
**멀티 테넌트 / 멀티 서버** 환경으로 고도화하여:

1. (1차) ClickEye 딜리버리 서빙 러너 서버들의 계정 풀을 중앙에서 배정·회수·모니터링하고,
2. (장기) 독립 서비스(SaaS)로 발전시킨다.

ClickEye와는 **코드 결합 없음**. ClickEye는 AMX의 조회 API를 읽는 외부 소비자일 뿐이다.

### 1.1 용어

| 용어 | 정의 |
|---|---|
| **AMS** | Account Management Server. 메인 관제 서버. 모니터링 + 테넌트/계정/서버/배정 CRUD + 명령 하달 |
| **AMA** | Account Management Agent. 하위 서버에 설치되는 에이전트 데몬(Go). tsamx 래퍼 + AMS 통신 |
| **테넌트** | 계정 그룹의 관리 경계. 계정·서버·배정은 모두 테넌트에 귀속 (SaaS 단계의 과금 경계) |
| **어카운트** | Claude 구독 계정 자격증명(OAuth credential 세트, §5.5). 테넌트 소유 |
| **배정(Assignment)** | "어느 어카운트를 어느 AMA 서버에 주입하는가"의 단위. 상태기계로 수명주기 관리 |
| **tsamx** | claude-swap v0.25.0b1의 내재화 포크 (`/tsamx`, MIT — `docs/TSAMX-GUIDE.md`). 로컬 계정 스위칭 엔진. AMA가 서브프로세스로 제어 |

### 1.2 확정된 핵심 결정 (Decision Log)

| # | 결정 | 내용 | 근거 |
|---|---|---|---|
| D1 | 독립 프로젝트 | ClickEye 내장이 아닌 별도 디렉토리/레포(AMX) | 사용자 확정 |
| D2 | 동시성 모델 | **단일 활성 + 스위칭** (tsamx 모델 그대로). 한 시점 1계정 활성, 95% 도달 시 교체 | 사용자 확정 |
| D3 | 자격증명 전달 | **AMS push 모델**. AMS가 중앙 OAuth 등록(§5.5)으로 획득한 **완전한 credential 세트**(access+refresh+expiresAt+전체 scopes+계정 메타)를 암호화해 gRPC로 전달 → AMA가 credential 파일 기록 + `tsamx add`로 등록. ⚠ setup-token/`add-token` 경로는 대화형 Claude Code가 로그인으로 인정하지 않아 폐기 (검증 2026-08-07) | 사용자 확정 (2026-08-07 개정) |
| D4 | 레포 형태 | 단일 모노레포 (contracts SSOT) | 사용자 확정 |
| D5 | AMS 스택 | FastAPI + PostgreSQL + Next.js 콘솔 | 사용자 확정 |
| D6 | AMA 스택 | **Go** (정적 바이너리 배포, gRPC/암호화 표준 지원) | 사용자 확정 |
| D7 | tsamx 재사용 | **CLI 서브프로세스 래핑** (Go 전환으로 라이브러리 import 불가) | D6 파급 |
| D8 | 스위칭 구동 | `tsamx auto` 상시 데몬 금지. **AMA가 `tsamx auto --once`를 틱 호출** (결정자 단일화) | D7 파급 |
| D9 | 통신 | gRPC bidi 스트림 1차, transport 인터페이스로 추상화(교체 가능). **AMA outbound-only** | 요구사항 5 |
| D10 | 임계치 | 자동 스위칭 95% (`tsamx config set autoswitch.threshold 95`) | 요구사항 AMA-3 |
| D11 | tsamx 배포 | 별도 레포 분리 없이 **모노레포 서브디렉터리 git 설치**: `uv tool install "git+<AMX 레포 주소>@<태그>#subdirectory=tsamx"` (D4 모노레포 SSOT 유지) | 사용자 확정 (2026-08-07) |

---

## 2. 선행 분석 — tsamx 엔진 (claude-swap 포크)

> 본 분석은 claude-swap v0.24.1 기준으로 수행했고, 내재화된 tsamx는 v0.25.0b1 기반이다.
> 동작 메커니즘·CLI 표면은 동일하나 **소스 라인 번호 인용은 v0.24.1 기준 근사치**다.

### 2.1 정체

- 모노레포에 내재화된 Python CLI 패키지 (`/tsamx`, uv tool로 설치). Claude Code 플러그인/훅이 **아님**.
- 소스 규모 ~17.6k LOC: `cli.py`, `autoswitch.py`, `switcher.py`, `credentials.py`,
  `usage_store.py`, `oauth.py`, `settings.py`, `poll_policy.py`, `paths.py`, `tui/`
- 통상 systemd user service(`tsamx auto`)로 상시 구동되나, **AMX에서는 이 데몬을 쓰지 않는다** (D8).

### 2.2 핵심 동작 메커니즘

- **데이터 루트**: `~/.local/share/tsamx/` (XDG, `CLAUDE_CONFIG_DIR`/`XDG_DATA_HOME` env로 재지정 가능)
  - `sequence.json` — 슬롯 순서 + 계정 메타(email, uuid, organization 등)
  - `credentials/.creds-<slot>-<email>.enc` — 계정별 자격증명 사본.
    **⚠ Linux에서는 base64 인코딩일 뿐 암호화가 아님** (`credentials.py:800`). macOS만 Keychain.
  - `configs/.claude-config-<slot>-<email>.json` — 계정별 `~/.claude.json` 사본
  - `autoswitch_state.json` — `{lastSwitchAt, lastSwitchTo, quarantine{}}` (스위칭 이벤트 감지 포인트)
  - `cache/usage.json` — 사용량 캐시 (schemaVersion 2)
- **limit 감지**: OAuth 토큰으로 `api.anthropic.com/api/oauth/usage` 폴링 →
  5시간/7일 윈도우 사용률의 최댓값(binding-window utilization) 산출
- **스위칭 절차**: utilization ≥ threshold → 후보 랭킹(strategy: best / next-available) →
  현재 계정 백업 → 대상 계정 credential/config를 `~/.claude/.credentials.json`, `~/.claude.json`에 복원
- **안정화 장치**: 히스테리시스(임계선 진동 방지), 쿨다운, 실패 계정 격리(quarantine),
  적응형 폴링 주기, 파일락

### 2.3 AMX가 사용하는 CLI 표면 (제어 계약)

| AMX 동작 | tsamx 명령 |
|---|---|
| 어카운트 등록 (D3 push 수신 후) | `tsamx add` — 현재 활성 credential을 슬롯으로 가져오기(`switcher.py:2114`). 슬롯 번호는 tsamx가 자동 할당하며 AMS는 관여하지 않는다(매핑 키는 email/uuid, §2.4-3). 전체 절차는 **§6.3 deliver가 SSOT** |
| 어카운트 회수 | `tsamx remove <num\|email>` |
| 활성화 (로테이션 복귀) | `tsamx enable <num\|email>` |
| 비활성화 (로테이션 제외) | `tsamx disable <num\|email>` |
| 수동 스위칭 | `tsamx switch <num\|email>` / `tsamx switch --strategy best` |
| 자동 스위칭 1틱 (D8) | `tsamx auto --once` |
| 사용량/상태 조회 (JSON) | `tsamx list --json`, `tsamx status --json` |
| 임계치 설정 (D10) | `tsamx config set autoswitch.threshold 95` |

### 2.4 설계에 반영한 tsamx의 한계

1. **로컬 암호화 부재**(Linux) → AMA가 자체 암호화 계층 제공 (§6.2)
2. **단일 활성 크레덴셜 모델** → D2로 수용 (병렬 실행은 비범위)
3. slot 번호는 remove 시 변동 → AMS↔AMA 계정 매핑 키는 slot이 아닌 **email/uuid** 사용
4. 스위칭 이벤트 콜백을 CLI로는 받을 수 없음 → 틱 전후 상태 비교 + `autoswitch_state.json` 감시로 대체 (§5.4)
5. **`add-token`은 등록 경로로 부적합** (실측 2026-08-07): setup-token을
   `{"claudeAiOauth": {accessToken, scopes: ["user:inference"]}}` 형태로만 저장
   (`switcher.py:101, 2409-2414`) — refreshToken/expiresAt/`user:profile` scope/계정 메타 부재로
   대화형 Claude Code가 스위칭 직후 로그인을 요구한다. → 완전한 credential 세트 기록 + `tsamx add` 사용 (D3)

---

## 3. 전체 아키텍처

```
┌────────────────────────────────────────────────────────────┐
│  AMS (관제면, 클라우드/사내 서버)                              │
│  ┌──────────────┐  ┌──────────────────────────────────┐    │
│  │ ams-web       │  │ ams-server (FastAPI)             │    │
│  │ Next.js 콘솔  │──│  REST API (관리 CRUD)             │    │
│  │ 모니터링/CRUD │  │  gRPC bidi 서버 (AMA 세션)         │    │
│  └──────────────┘  │  정책/배정 엔진 + 재조정(reconcile) │    │
│                    └───────────┬──────────────────────┘    │
│                    PostgreSQL ─┘  (테넌트/계정/서버/배정/사용량)│
└────────────────────────┬───────────────────────────────────┘
                         │  gRPC bidi 스트림 (TLS, 443)
                         │  ← AMA가 outbound로 다이얼 (인바운드 포트 불필요)
                         │  ↓ 하향: 명령 (deliver/recall/activate/…)
                         │  ↑ 상향: 등록/하트비트/사용량 보고/이벤트/ack
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ AMA 서버 A    │ │ AMA 서버 B    │ │ AMA 서버 C    │   (테넌트:서버 = 1:N)
│ 어카운트 3개  │ │ 어카운트 5개  │ │ 어카운트 2개  │   (배정 예: 10개 중 3/5/2)
│ ┌──────────┐ │ │              │ │              │
│ │ama-agent  │ │ │      …       │ │      …       │
│ │(Go 데몬)  │ │ │              │ │              │
│ │ ├ gRPC 클라이언트 (재연결·백오프)                │
│ │ ├ 명령 핸들러 (멱등)                            │
│ │ ├ 암호화 로컬 스토어 (매니페스트)                │
│ │ ├ tsamx 브리지 (서브프로세스)                    │
│ │ └ 리포터 (5분 폴링 + 즉시 이벤트)                │
│ └────┬─────┘ │
│      ▼ CLI    │
│ ┌──────────┐ │
│ │ tsamx     │ │  add / remove / enable / disable
│ │ (Python)  │ │  switch / auto --once / list --json
│ └────┬─────┘ │
│      ▼        │
│ ~/.claude/.credentials.json  ← 단일 활성 크레덴셜 (D2)
│ (Claude Code / 러너가 이 자격증명으로 실행)
└──────────────┘
```

**신뢰 모델 요약**
- 명령 권위: AMS만 계정 변경을 지시할 수 있다. AMA는 AMS Ed25519 서명을 검증한 명령만 수행한다.
- AMA는 스스로 계정 구성을 바꾸지 않는다(예외: `auto --once`에 의한 스위칭 — 이는 AMS가 지정한
  모드(auto) 안에서의 동작이며 즉시 AMS에 보고된다).
- 사용자가 root인 서버에서 "임의 변경 절대 불가"는 원리적으로 달성 불가 →
  실질 보장선: 오프박스 사본 무용화 + 변조 탐지 + **AMS의 desired-vs-actual 재조정**(§6.4).

---

## 4. 프로젝트 구조 (모노레포)

```
AMX/
├── AMX-DESIGN.md            # 본 문서 (설계 SSOT)
├── contracts/               # 계약 SSOT — 여기가 항상 먼저 바뀐다
│   ├── proto/
│   │   └── amx.proto        # gRPC 서비스/메시지 정의
│   ├── schemas/             # 보고 JSON 스키마 (usage-report, switch-event)
│   └── gen/                 # 생성 코드 (go / python / typescript)
├── ams-server/              # FastAPI + PostgreSQL
│   ├── app/
│   │   ├── api/v1/          # REST 라우터 (tenants, accounts, servers, assignments)
│   │   ├── grpc/            # gRPC bidi 서버 + 세션 레지스트리
│   │   ├── models/          # SQLAlchemy 모델
│   │   ├── schemas/         # Pydantic 스키마
│   │   ├── services/        # 배정 엔진, 재조정(reconcile), 사용량 인제스트
│   │   ├── transport/       # AmaTransport 포트 + gRPC 어댑터 (교체 가능 지점)
│   │   └── core/            # 암호화(AMX_ENCRYPTION_KEY), Ed25519 서명, 인증
│   ├── alembic/             # DB 마이그레이션
│   └── tests/
├── ams-web/                 # Next.js 관제 콘솔
│   └── src/
│       ├── app/(dashboard)/ # 테넌트/계정/서버/배정 CRUD + 모니터링 대시보드
│       └── lib/api-client/  # contracts 생성 타입 사용
├── ama-agent/               # Go 데몬
│   ├── cmd/ama/             # main
│   ├── internal/
│   │   ├── transport/       # gRPC 클라이언트 (다이얼·재연결·백오프) — 포트 인터페이스
│   │   ├── command/         # 명령 핸들러 (멱등, cmdId 처리로그)
│   │   ├── store/           # 암호화 매니페스트 (AES-256-GCM)
│   │   ├── tsamx/           # tsamx CLI 브리지 (exec + JSON 파싱 + state 감시)
│   │   ├── reporter/        # 5분 폴링 리포트 + 즉시 이벤트 + 오프라인 아웃박스
│   │   └── crypto/          # 복호화, Ed25519 검증
│   └── installer/           # 설치 스크립트 (내재화 tsamx 패키지 설치 포함)
├── tsamx/                   # 내재화 스위칭 엔진 (claude-swap 포크, docs/TSAMX-GUIDE.md)
├── vendor/
│   └── claude-swap-upstream/ # 업스트림 원본 스냅샷 (v0.25.0b1, 향후 diff 병합용)
└── infra/                   # docker-compose (postgres, ams-server, ams-web), 배포 설정
```

**계약 우선 원칙**: API/프로토콜 변경은 반드시 `contracts/` 먼저 → 코드 생성 → 각 모듈 반영.

---

## 5. AMS 설계 (관제면)

### 5.1 도메인 모델 / ERD

```
Tenant ──1:N──► Account      ──┐
   │                            │
   ├──1:N──► Server (AMA)    ──┼──► Assignment (Account × Server, 테넌트 내부에서만)
   │                            │
   └──1:N──► UsageSnapshot ◄───┘   (보고 수집 원장)
```

```sql
-- 핵심 스키마 (요지)
tenants (
  id UUID PK, name, status, created_at, updated_at
)

accounts (
  id UUID PK,
  tenant_id UUID FK → tenants,
  email TEXT,                          -- tsamx 매핑 키 (slot 아님, §2.4-3)
  credential_type TEXT,                -- oauth | api_key  (setup_token 폐기, D3)
  encrypted_secret TEXT,               -- credential 세트 JSON 봉투(§5.5 access+refresh+expiresAt+scopes+계정 메타). 봉투암호화(F2): `v2:{dek_ver}:{nonce}:{ct}` = 테넌트 DEK로 AES-256-GCM(AAD=tenant_id), 레거시는 접두 없는 Fernet(전환 병행). §7
  status TEXT,                         -- available | assigned | disabled | quarantined
  last_switched_at TIMESTAMPTZ,
  UNIQUE (id, tenant_id)               -- ★ 격리 앵커
)

servers (
  id UUID PK,
  tenant_id UUID FK → tenants,         -- 테넌트:서버 = 1:N (요구 AMS-4)
  name, hostname,
  enroll_token_hash TEXT,              -- 1회성 등록 토큰 (해시만 저장)
  server_cred_hash TEXT,               -- 장수명 서버 자격증명 (교환 후)
  switch_mode TEXT,                    -- auto | manual  (서버 단위 — tsamx auto가 풀 단위이므로)
  status TEXT,                         -- online | offline | degraded
  last_seen_at TIMESTAMPTZ,
  cpu_pct, mem_pct, disk_pct DOUBLE PRECISION,  -- 최근 하트비트 호스트 사용률(§5.4 호스트 메트릭), 0..100, NULL=미보고
  metrics_reported_at TIMESTAMPTZ,     -- 위 3값의 신선도 스탬프
  UNIQUE (id, tenant_id)               -- ★ 격리 앵커
)

assignments (
  id UUID PK,
  tenant_id UUID NOT NULL,
  account_id UUID, server_id UUID,
  state TEXT,                          -- §5.2 상태기계
  pinned BOOLEAN DEFAULT false,        -- auto 로테이션 개별 제외
  delivered_at, acked_at, last_error,
  FOREIGN KEY (account_id, tenant_id) REFERENCES accounts (id, tenant_id),  -- ★
  FOREIGN KEY (server_id,  tenant_id) REFERENCES servers  (id, tenant_id),  -- ★
  UNIQUE (tenant_id, account_id) WHERE state != 'detached'  -- 한 계정 = 동시 1서버
)

usage_snapshots (
  id UUID PK, tenant_id, server_id, account_id NULL,
  report_type TEXT,                    -- usage | switch_event
  payload JSONB,                       -- 보고 원문(usage 행은 보존 정책 대상, 아래)
  reported_at TIMESTAMPTZ
)

-- 스냅샷 보존 정책 (retention sweep, usage_cost.sweep_snapshot_retention)
-- 원문 스냅샷은 서버당 ~5분마다 무한 적재되므로 주기 삭제한다. 기본 90일
-- (`usage_snapshot_retention_days`, 0 이하면 비활성). 삭제 조건은 두 가지를 모두
-- 만족하는 `report_type='usage'` 행만이다: ① reported_at < now-보존일, ②
-- reported_at < 정산 boundary(= rollup·billing 두 watermark의 min). 즉 미정산
-- 스냅샷은 기한이 지나도 절대 삭제하지 않는다. 정산 boundary가 미래에 주차된
-- 경우(G27 시계 점프)는 그 아래가 영원히 미정산이므로 purge를 전면 중단·경고만
-- 남긴다. 다만 rollup·billing 스윕 자체가 주차(`start > last_closed_end`)를
-- 감지하면 커서를 `last_closed_end`로 되감아 자가치유하므로(다음 틱부터 재정산
-- 재개) 이 중단은 되감김이 반영되기 전 과도기 구간의 안전망으로만 작동한다.
-- switch_event 행은 콘솔 이벤트 타임라인(list_switch_events →
-- GET /servers/{id}/switch-events)의 유일한 원천이라 기한·정산과 무관하게 보존한다.
-- usage_daily_rollup은 이 정책의 대상이 아니며 영구 보존한다(비용 배분 입력).
-- 단, 90일을 넘긴 과거일은 원문이 사라지므로 usage_daily_rollup에 봉인된 값으로만
-- 답할 수 있고 원문 기반 재계산(적분 규칙 변경 후 recompute)은 더 이상 불가하다.

-- 관리자 RBAC (P5 F1, §7)
admins (
  id UUID PK,
  email TEXT, UNIQUE(lower(email)),
  password_hash TEXT,                  -- bcrypt (sha256 프리해시)
  role TEXT,                           -- global-admin | tenant-admin
  tenant_id UUID NULL FK → tenants,    -- tenant-admin의 소속 테넌트(격리 앵커), ON DELETE RESTRICT
  disabled BOOLEAN DEFAULT false,
  CHECK ((role='global-admin' AND tenant_id IS NULL)
      OR (role='tenant-admin' AND tenant_id IS NOT NULL))
)

admin_sessions (
  id UUID PK,
  admin_id UUID FK → admins ON DELETE CASCADE,
  token_hash TEXT UNIQUE,              -- sha256(raw); raw는 발급 시 1회만 반환
  expires_at TIMESTAMPTZ
)

-- 봉투암호화 DEK (P5 F2, §7)
tenant_deks (
  id UUID PK,
  tenant_id UUID FK → tenants ON DELETE RESTRICT,
  version INT,                         -- lazy 재암호로 구/신 버전 공존; 활성 = retired_at NULL 중 max
  wrapped_dek BYTEA,                   -- DEK를 KEK provider로 래핑(AAD=tenant_id)
  kek_provider TEXT,                   -- local | aws-kms | vault
  kek_key_id TEXT,                     -- provider별 키 식별자(KMS ARN 등)
  algorithm TEXT DEFAULT 'AES-256-GCM',
  created_at TIMESTAMPTZ, retired_at TIMESTAMPTZ NULL,
  UNIQUE (tenant_id, version)
)

-- 내부 청구 outbox (P5 F5, usage_snapshots 원장 기반; 외부 결제 연동 없음)
billing_events (
  id UUID PK,
  tenant_id UUID FK → tenants ON DELETE CASCADE,
  kind TEXT,                            -- usage_daily | usage_daily_void | usage_daily_reagg (G26 정정)
  period_start, period_end TIMESTAMPTZ, -- 닫힌 UTC 일 경계 [D, D+1)
  payload JSONB,                        -- account_days · account_ids · server_count · snapshot_count · max_utilization_pct
  status TEXT DEFAULT 'pending',        -- pending | exported
  exported_at TIMESTAMPTZ NULL, created_at TIMESTAMPTZ,
  UNIQUE (tenant_id, kind, period_start),  -- 멱등성 앵커(스윕 ON CONFLICT DO NOTHING)
  INDEX (tenant_id, status)
)

billing_cursors (
  kind TEXT PK,                         -- usage_daily
  watermark TIMESTAMPTZ,                -- 마지막으로 집계한 닫힌 일의 끝(다음 스윕 시작점)
  updated_at TIMESTAMPTZ
)
```

**void/재집계 시맨틱 (G26)** — export 후 정정은 원본 불변. `POST …/void`가 exported `usage_daily`를
반전하는 `usage_daily_void` 이벤트(payload에 원본 id·집계 참조)를 남기고, 같은 일을 현재
snapshot으로 재집계한 신규 pending `usage_daily_reagg`를 원자적으로 생성한다. 세 kind가 같은
`period_start`를 공유해도 `UNIQUE(tenant_id, kind, period_start)`로 공존하며, 일 순합 = 원본 − void +
reagg. 스윕 워터마크 멱등은 불변(해당 일은 워터마크 뒤). 삭제 가드: pending 이벤트가 있으면 테넌트
삭제 거부(G25), exported만 남으면 허용.

**★ 테넌트 격리 불변식 (요구 AMS-7)**
Account·Server의 `UNIQUE(id, tenant_id)`를 앵커로, Assignment가 **복합 FK 2개**로 참조한다.
Assignment 한 행의 `tenant_id`는 하나이므로, 계정과 서버가 서로 다른 테넌트면
두 FK를 동시에 만족할 수 없어 **INSERT가 DB 수준에서 거부된다**.
애플리케이션 검증에 의존하지 않는 구조적 불변식이며, 서비스 계층 검증은 이중 방어로 추가한다.

### 5.2 배정(Assignment) 상태기계 (요구 AMS-6)

```
                    deliver          ack.ok
        pending ──────────► delivering ──────► active
           ▲                    │                │ ▲
           └────────────────────┘                │ │ activate
             ack.fail / timeout (재시도)  deactivate │
                                                 ▼ │
                                              inactive
        active ◄──── recover ──── quarantined ◄── AMA 이벤트(소진/실패)

        {active, inactive, quarantined} ── recall ──► recalling ── ack.ok ──► detached
                                                                              (감사용 행 유지)
```

| 상태 | 의미 | AMA 측 대응 |
|---|---|---|
| pending | 생성됨, 미하달 | — |
| delivering | deliver 명령 전송, ack 대기 | credential 기록 + `tsamx add` 수행 중 |
| active | 하달·확인 완료, 스위칭 후보 | `tsamx enable` 상태 |
| inactive | 하달됐으나 로테이션 제외 | `tsamx disable` 상태 |
| quarantined | AMA가 소진/실패 보고 | tsamx quarantine 반영 |
| recalling | 회수 명령 전송, 확인 대기 | `tsamx remove` 수행 중 — 항상 `purge_local_copy=true`(O2 변경 2026-08-14): 로컬 credential·매니페스트 레코드 완전 삭제 |
| detached | 종말 상태 (행은 감사용 유지) | 로컬 흔적 완전 제거됨; 이력은 detached 배정 행·이벤트로만 남는다(재배정은 재전달) |

- **스위칭 모드**는 서버 단위 속성(`servers.switch_mode`), 계정 단위 제외는 `pinned`로.
- **배정 제외**: 사람이 자기 프로필에서 직접 쓰는 계정은 `accounts.assignment_excluded`로
  표시한다. 표시된 계정은 신규 배정이 거부되는데, 같은 OAuth refresh token 회전을 두 곳이
  경합하면 양쪽 다 깨지기 때문이다(2026-08-17 관측). opt-in이라 기본값은 배정 가능이고,
  표시를 세워도 기존 배정은 그대로 남으며 그 배정의 재전달(reconcile `CORRECTION_REDELIVER`)도
  계속된다 — 사고를 실제로 멈추려면 회수까지 해야 한다.
- **비상태 명령**: `switch_now`/`set_mode`/`req_report`는 배정 상태를 전이시키지 않는다
  (switch-now는 `last_switched_at`만 갱신, set_mode는 `servers.switch_mode`만 변경).
- **recover 전이**: REST `POST …/assignments/{id}:recover`(§5.3)가 트리거 →
  AMA에 `set_active(activate)` 하달 → ack 시 quarantined → active.
- **재배정 (O2 변경 2026-08-14)**: recall이 항상 purge로 통일되면서 회수된 계정은
  로컬에 흔적을 남기지 않는다. 따라서 같은 서버로의 재배정도 예외 없이 full deliver
  (credential 재전송)로 수행된다. 이전의 `tsamx enable` 단축 경로(보존 레코드 재사용)는
  폐기됐다.
- **recall 실패 회복 (D1)**: recall이 DIVERGED/REJECTED로 실패하면 배정은 `recalling`에
  머물되 `pending_command_id=NULL`(정착·비인플라이트 표식)이 되고, 확인 경로에서 계정 스코프
  `recall_failed` 경보(dedupe `server_id:recall_failed:account_id`)를 연다. 정착 recalling은
  ① reconcile-on-report가 로컬 잔존 시 자동 재recall(`CORRECTION_CAP`, 기본 3)·이미 제거됐으면
  `detached`로 정착, ② REST `POST …:recall`로 수동 재요청(`recall_retry_count` 상한
  `AMX_MAX_RECALL_RETRIES`, 기본 3)으로 회복된다. 자동·수동 상한은 **분리**되어 총 재시도 예산은
  6이며, 재recall이 CONVERGED로 성공하면 카운터·경보가 자동 해소된다. 인플라이트
  recall(`pending_command_id` 존재)은 회복 대상에서 제외한다.
  - **상한 초과 종착지**: 두 상한이 모두 소진되면 자동 회복은 멈추고 `recall_failed` 경보만
    유지된다(무한 재발행 방지). 이 배정은 `recalling`이라 `detached`가 아니어서 계정·서버 삭제도
    막히므로, 최종 탈출구로 global-admin이 `POST …:recall {"force": true}`를 발행한다 —
    force는 상한을 우회하고 `recall_retry_count`를 0으로 리셋해 재무장하며, 성공하면 배정이
    `detached`로 정착해 stranded가 해소된다.
- **미ack 명령 회복 (D2)**: 에이전트가 명령 수신 후 ack 전 끊겨 `agent_commands`가 `sent`로
  남으면, 타임아웃(기본 3×heartbeat) 스윕이 `queued`로 재큐해 멱등 재전송(같은 command_id)하고,
  `send_attempts` 상한 초과 시 `failed`로 두며 배정을 재발행 가능 상태로 되돌린다.

### 5.3 REST API 표면 (관리 CRUD — 요구 AMS-1~4, 6)

| 메서드·경로 | 용도 |
|---|---|
| `POST/GET /api/v1/tenants` · `GET/PATCH/DELETE /api/v1/tenants/{tid}` | 테넌트 CRUD |
| `POST/GET /api/v1/tenants/{tid}/accounts` · `GET/PATCH/DELETE …/{aid}` | 어카운트 CRUD (secret은 write-only, 응답 항상 마스킹) |
| `POST /api/v1/tenants/{tid}/accounts:oauth-start` / `:oauth-complete` | 중앙 OAuth 등록 플로우 (§5.5): authorize URL 발급 / 코드 교환·저장 |
| `POST/GET /api/v1/tenants/{tid}/servers` · `GET/PATCH/DELETE …/{sid}` | AMA 서버 CRUD |
| `POST /api/v1/tenants/{tid}/servers/{sid}/enroll-token` | 1회성 등록 토큰 발급 |
| `POST/GET /api/v1/tenants/{tid}/assignments` · `DELETE …/{id}` | 배정 생성·목록 (예: 10개 중 A:3 / B:5 / C:2) · detached 이력 행 삭제(그 외 상태는 409, §5.2) |
| `GET /api/v1/tenants/{tid}/audit-logs` | 변경성 관리 액션 감사 로그 조회 (§5.6.4) |
| `POST …/assignments/{id}:deliver` / `:recall` / `:activate` / `:deactivate` / `:recover` | 상태 전이 (`:recover`는 quarantined → active 복귀, §5.2) |
| `POST …/assignments/{id}:switch-now` | 수동 스위칭 (특정 계정으로) |
| `POST …/servers/{sid}:switch-mode` | auto ↔ manual 전환 |
| `GET …/servers/{sid}/usage` | 최신 사용량 조회 (DB 캐시) |
| `POST …/servers/{sid}:refresh-usage` | AMA에 즉시 보고 요청 (요구 AMA-2 수동 조회) |

### 5.4 AMS↔AMA 통신 (요구 AMS-5)

**방향**: AMA가 AMS로 **outbound 다이얼**(TLS, 443)하여 장수명 gRPC bidi 스트림을 연다.
AMS는 그 스트림으로 명령을 하달한다. 고객 방화벽/NAT에서 인바운드 포트가 불필요하다.

**추상화**: AMS 도메인 로직은 `AmaTransport` 포트(명령 push / 보고 ingest)에만 의존.
gRPC는 어댑터 1개일 뿐이며, WebSocket 등 다른 어댑터로 교체 가능(요구사항의 "다른 통신 프로토콜" 대비).

```proto
// contracts/proto/amx.proto (스케치)
service AmxControlPlane {
  // AMA가 개설하는 단일 장수명 스트림: 상향=보고, 하향=명령
  rpc Session(stream AmaMessage) returns (stream AmsCommand);
  // 스트림 미가용 환경 폴백
  rpc ReportUsage(UsageReport) returns (Ack);
}

message AmsCommand {
  string command_id = 1;              // 멱등키
  bytes  signature  = 2;              // AMS Ed25519 서명 (AMA가 내장 공개키로 검증)
  oneof cmd {
    DeliverAccount   deliver     = 10;  // 암호화 credential 세트 포함 (D3). slot 필드 없음 — AMA 로컬 자동 할당
    RecallAccount    recall      = 11;
    SetAccountActive set_active  = 12;  // activate / deactivate
    SetSwitchMode    set_mode    = 13;  // auto / manual
    SwitchNow        switch_now  = 14;  // 수동 스위칭 대상 지정
    RequestReport    req_report  = 15;  // 즉시 사용량 보고 요청
    SelfUpdate       self_update = 18;  // 자기 저장소 ff-only pull + 재빌드 + 재기동. 소스 지정 필드 없음
  }
}

message AmaMessage {
  oneof msg {
    Register    register = 10;  // 연결 직후: server credential 제시
    Heartbeat   hb       = 11;
    UsageReport usage    = 12;  // 5분 폴링 or req_report 응답
    CommandAck  ack      = 13;  // 명령 결과 = "수렴 상태" 회신
    AccountEvent event   = 14;  // 스위칭 / 전체소진 / 격리 (즉시)
  }
}
```

**재조정(Reconcile) 루프**: AMS는 배정 테이블(desired)과 AMA 보고(actual)를 주기 비교.
불일치(드리프트) 감지 시 경보 + 교정 명령 재하달. 이것이 "AMA 임의 변경 불가"의 실질 집행자다.

**다중 AMS 인스턴스 (P5 F3)**: 명령 전달은 DB-큐로 디커플돼 AMS는 라우팅 관점 **stateless**다.
각 인스턴스가 `agent_commands`를 `FOR UPDATE SKIP LOCKED`로 폴링하고 fetch→claim(`sent`)을 **단일
트랜잭션에 커밋**해, 재연결 순간 stale+신규 세션이 같은 server를 동시 폴링해도 각 명령을 한 인스턴스만
하달한다(중복 방지, 잔여 경합은 멱등 command_id 백스톱). 배경 스위퍼(offline/sent-ack 타임아웃)는
트랜잭션 스코프 **advisory lock**(`pg_try_advisory_xact_lock`)으로 중복 실행을 배제한다. 세션 레지스트리·
내부 라우팅은 불요. gRPC 세션 presence 공유(직접 push 최적화)는 미도입(O7, SaaS 단계).
(claim-before-write 특성상 write 실패한 명령은 즉시 재전송이 아니라 sent-ack 타임아웃(§D2, 기본 90s)으로
복구된다 — 멱등이라 유실은 없고 지연만.) 재큐잉은 `MAX_SEND_ATTEMPTS`(기본 5)까지, 소진 시 명령을
`failed`로 확정하고 배정을 재발행 가능한 resting 상태로 되돌린다. 경보는 **account-scoped 명령의
최종 실패에만** 개방한다 — recall 계열은 D1 `recall_failed` 재사용, 그 외(deliver/activate/deactivate/
switch_now)는 신규 `command_send_failed`, 모두 `server:kind:account` 키. 같은 대상의 후속 명령이
CONVERGED로 acked되면 auto-resolve. **서버-scoped 명령**(set_mode/set_policy/req_report)의 최종
실패는 경보를 열지 않는다 — 다음 세션의 정책 재-assertion이 의도를 재적용하는 자가치유 부류라 침묵이
설계 의도다(수동 경보 영구 누적·3종 dedupe 키 공유 방지). **예외는 `self_update`**: 서버-scoped지만
재-assertion으로 낫지 않고(다음 세션이 다시 빌드해주지 않는다) 되돌릴 배정도 없어서, 실패 ack이
`self_update_failed`(`server:kind` 키, account 없음)를 연다. 나중 self_update가 CONVERGED로 acked되면
auto-resolve. 승계 명령이 배정의 pending 마커를 이미
가져간 구(舊) 명령도 경보를 열지 않는다(승계 명령이 자기 결과로 보고). D2 판정 갭 2건(수용):
- **갭3 오프라인 서버**: report가 오지 않으면 reconcile-on-report에 도달하지 못하나, 오프라인 서버는
  `server_offline` 경보(§5.6, `last_seen_at` 스위퍼)가 커버한다. `command_send_failed`는 account-scoped
  이므로 `server_offline`과 스코프가 달라 이중 개방이 아니다.
- **갭4 flapping 창**: 타임아웃(90s)×cap(5) 누적으로 최악 ~12분 동안 경보 미개방/지연 창이 존재할 수
  있으나, 멱등 재큐잉이 그 사이 대부분을 흡수하므로 수용한다.

**호스트 메트릭 (하트비트 편승)**: 하트비트(`Heartbeat`, 30초 주기)에 선택적 `SystemMetrics`를 실어
`cpu_pct`/`mem_pct`/`disk_pct`(각 0..100)를 상향한다. AMA가 `/proc/stat`(두 샘플 델타)·`/proc/meminfo`·
`statfs`로 외부 의존성 없이 수집하며, AMS는 값이 실제로 실린 하트비트에서만 `servers`의 3컬럼과
`metrics_reported_at`를 갱신한다. 서브메시지 presence(`HasField`)로 판별하므로, 필드를 보내지 않는 구
에이전트·비Linux 호스트·수집 실패는 컬럼을 건드리지 않는다 — NULL은 "미보고"이지 "0%"가 아니다.
- **신선도**: 값의 최신성은 `metrics_reported_at`로 판정한다. 서버 `status != online`이면 콘솔은 그 값을
  stale로 취급한다(마지막 갱신 이후 하트비트가 끊긴 것이므로).
- **폴백 전용 서버**: unary `ReportUsage`로만 생존하는 서버는 하트비트 스트림이 없어 메트릭이 갱신되지
  않는다(의도). 그런 서버의 3컬럼은 마지막 스트림 세션 값에 머물거나 계속 NULL이다.
- **측정 전제**: WSL·컨테이너에서는 CPU·MEM이 게스트 VM 기준, DISK는 마운트에 따라 호스트/게스트가
  섞여 실호스트 자원과 어긋난다. 값은 tsamx가 실제로 도는 서버에 에이전트를 **직접 설치**했을 때만
  의미가 있다. 데이터 볼륨이 루트와 다르면 `AMX_METRICS_DISK_PATH`로 statfs 대상을 지정한다(기본 `/`).

### 5.5 어카운트 등록 — AMS 중앙 OAuth 플로우

계정당 필요한 브라우저 로그인 1회를 **AMS 콘솔에서** 수행한다. AMA 서버에서는 로그인 0회.
OAuth authorize URL은 CLI가 아니라 누구든 생성 가능한 값이며, tsamx조차 동일 상수로
직접 HTTP 호출한다(`oauth.py:18-19` — token URL `https://platform.claude.com/v1/oauth/token`,
client_id `9d1c250a-e61b-44d9-88ed-5944d1962f5e`).

```
1. 관리자: ams-web "어카운트 추가" 클릭
2. AMS: PKCE 쌍(verifier/challenge) 생성 → authorize URL 표시
   (verifier는 서버 세션에 보관 — URL 생성자만 코드 교환 가능)
3. 관리자: 브라우저 로그인 → 발급된 코드를 콘솔에 붙여넣기
4. AMS: 코드 + verifier로 토큰 교환 → 완전한 credential 세트 획득
   (accessToken + refreshToken + expiresAt + 전체 scopes + 계정 UUID/조직 메타)
5. AMS: `accounts.encrypted_secret`에 암호화 저장 → 이후 D3 push로 배정
```

이 credential은 실제 `claude login` 산출물과 동일 형태이므로, 주입받은 서버의
Claude Code는 로그인과 구분할 수 없다. setup-token 경로가 실패한 이유는 §2.4-5.

### 5.6 콘솔·경보 (P4, 설계노트 `docs/design-notes/p4-architecture.md`)

- **ams-web = BFF 강제**: Next.js Route Handler를 통해서만 REST 호출. 관리자 Bearer 토큰은
  Next 서버 프로세스 env에만, **브라우저에 노출 금지**. 브라우저↔BFF는 httpOnly·SameSite=Strict
  세션 쿠키. 실시간 뷰는 폴링(5~10s, async 명령 수렴 모델에 정합; SSE는 확장점).
- **경보 `alerts` 테이블(신규)**: P3가 훅으로 이월한 all_exhausted를 여기서 소비. 소스 = all_exhausted
  이벤트 · reconcile 드리프트(`usage_snapshots.drift`) · 서버 오프라인. `dedupe_key`(server×kind[×account])로
  open 1건 유지, 다음 UsageReport에서 조건 해소 시 auto-resolve. `GET /alerts` · `POST /alerts/{id}:ack`.
  경보는 이벤트 유실에 강건하도록 **reconcile-on-report 재조회로도 재구성 가능**(§P3 결정8 best-effort 보완).
  - **P3 기준 경보 kind(8종. 전체 로스터는 14종 — P4·P5가 langfuse 3종·`alert_webhook_dropped`·`dangerous_command`를 §5.6.1~5.6.3에서, `credential_unusable`을 §5.7에서 더한다)**: 서버-scoped `all_exhausted`·`server_offline`·`self_update_failed`(`{server}:{kind}`),
    계정-scoped `drift`·`quarantine`·`recall_failed`·`command_send_failed`(`{server}:{kind}:{account}`),
    테넌트-scoped `billing_watermark_future`. 뒤 넷의 배선은 §5.2·회복 설계(§D1/D2)에, `billing_watermark_future`는
    watermark-future 스위퍼(§5.6.1 목록의 여섯 번째, 락 …06)가 `reported_at < watermark` 시계 앞점프를 감지해 여는
    것으로 G27 자가치유(§보존 정책 주석)의 관측 지점이다. P3 이후 더해진 계정-scoped `credential_unusable`
    (`{server}:{kind}:{account}`, alembic 0026)은 §5.7 토큰 재료 가드의 드롭을 경보로 올린다. 해소는 같은 계정의
    cred_update가 실제로 저장되는 시점이다. kind를 넓히는 마이그레이션이 아직 적용되지 않은 서버는 CHECK가
    INSERT를 거부하는데, 이벤트별 경보 쓰기를 격리해 뒀으므로 스트림은 끊기지 않고 그 경보 하나만 누락된다
    (스냅샷은 경보보다 먼저 커밋해 타임라인에 남는다).
- **서버 오프라인 정합**: `last_seen_at` 만료 스위퍼(주기적 `< now-3틱 → offline`) — half-open 스트림에서
  online 고착·경보 미발화 방지.
- **인증**: ams-server는 단일 관리자 Bearer 유지, BFF에 로그인→쿠키 세션 + `Principal` 반환형(P5 테넌트
  RBAC 훅 자리)만 추가. 멀티테넌트 RBAC는 P5.
- **완료판정 검증**: BFF API 레벨(Route Handler 프로그램 구동)로 전 수명주기 조작 판정 + OAuth 등록
  마법사·deliver/recall만 실브라우저 Playwright 스모크.

#### 5.6.1 Langfuse 사용량 롤업 (P4 콘솔 모니터링)

콘솔의 토큰 사용량 뷰는 외부 **Langfuse Metrics API**(`GET /api/public/v2/metrics`, Basic
Auth)를 주기 폴링해 `langfuse_usage_rollup`(PK `tenant_id, day, dimension, key`)에 적재한
로컬 롤업을 읽는다. 매 요청을 Langfuse로 프록시하지 않는다.

- **일곱 번째 스위퍼**: 기존 6종 배경 스위퍼(offline·sent-ack·billing·usage-rollup·
  snapshot-retention·watermark-future)에 이어 `services.langfuse_metrics.sweep_langfuse_metrics`를
  같은 30초 틱에 sibling으로 붙인다. 전용 transaction-scope advisory lock **`0x414D580F07`(…07)**
  로 다중 인스턴스 중복 적재를 배제하고, 자체 try/except로 다른 스위퍼와 격리한다.
- **배경 스위퍼·advisory lock 배정표(현행 전량)**: 위 "여섯 번째·일곱 번째" 서술은 추가 순서를 가리키는 역사적 표현이고, 지금 공유 틱 루프가 도는 스위퍼는 10종이다(락 키 접두 `0x414D580F`, 뒤 한 바이트로 구분). 웹훅 드레인 …08만 공유 루프가 아니라 독립 asyncio 태스크다(§5.6.2).

  | 락 | 스위퍼 | 하는 일 |
  |---|---|---|
  | …01 | `alerts.sweep_offline` | `last_seen_at` 만료 → 서버 offline 전이·경보 |
  | …02 | `commands.sweep_sent_timeouts` | sent-ack 타임아웃 명령 재큐 |
  | …03 | `billing.sweep_billing` | 마감일 usage → billing_events 롤 |
  | …04 | `usage_cost.sweep_usage_rollup` | 스냅샷 → usage 롤업 재집계 |
  | …05 | `usage_cost.sweep_snapshot_retention` | 보존기간 지난 usage 스냅샷 삭제 |
  | …06 | `usage_cost.sweep_watermark_future` | 워터마크 앞점프 감지 → `billing_watermark_future` |
  | …07 | `langfuse_metrics.sweep_langfuse_metrics` | Langfuse Metrics 폴링 → 롤업 적재 |
  | …08 | `alert_webhook.sweep_alert_webhook` | **독립 드레인 태스크** — 아웃박스 웹훅 발송(§5.6.2) |
  | …09 | `langfuse_alerts.sweep_langfuse_alerts` | Langfuse 임계값 경보 3종(§5.6.2) |
  | …0A | `inventory.sweep_assignment_retention` | 보존기간 지난 `detached` 배정 행 배치 삭제(`AMX_ASSIGNMENT_RETENTION_DAYS` 기본 90) |
  | …0B | `audit.sweep_audit_retention` | 감사 로그 배치 삭제(`AMX_AUDIT_RETENTION_DAYS`>0일 때만, §5.6.4) |
- **폴링 주기 분리**: 30초 틱마다 재폴링하면 외부 API를 과하게 때리므로, 프로세스-로컬 monotonic
  게이트로 `AMX_LANGFUSE_POLL_SECONDS`(기본 300, 최소 60) 미만 간격의 틱은 즉시 return한다.
  인스턴스 간 조율은 …07 락이 담당하므로 게이트는 프로세스 로컬로 충분하다.
- **2단계 구조**: 모든 HTTP GET을 먼저 락·트랜잭션 밖에서 수행해 메모리에 모으고, 그 뒤 …07 락을
  잡아 upsert+commit만 짧게 처리한다. 느리거나 막힌 Langfuse가 DB 트랜잭션이나 크로스-인스턴스
  락을 붙잡지 못하게 한다. HTTP 오류·JSON 파싱 오류(`ValueError`)는 시크릿을 로그에 남기지 않고
  해당 틱을 중단하되, 앞서 모은 날짜는 그대로 적재한다(멱등이라 다음 주기에 재롤).
- **집계 축·윈도우**: `observations` 뷰를 일 단위로 `dimension="model"`(providedModelName 그룹,
  null 모델은 `key="unknown"`)과 `dimension="user"`(userId는 고카디널리티라 그룹 불가·필터만
  가능 → 테넌트 계정 이메일을 userId 필터로 고정해 루프) 두 축으로 뽑는다. 재집계 윈도우는
  `AMX_LANGFUSE_METRICS_WINDOW_DAYS`(기본 3)이며 **최소 2로 클램프**한다 — 윈도우 1이면 오늘(항상
  미확정)만 롤해 마감된 날의 확정치가 영구 미저장되므로, 오늘+어제를 덮어 마감일이 반드시 한 번
  재롤되게 한다. 계정 수는 `AMX_LANGFUSE_MAX_ACCOUNTS`(기본 100)로 상한, 초과 시 경고 후 정렬
  선두 N개만 처리한다.
- **전역 테넌트 바인딩**: 활성화는 `AMX_LANGFUSE_BASE_URL`/`PUBLIC_KEY`/`SECRET_KEY`/`TENANT_ID`
  4종이 모두 설정된 경우에 한한다(all-or-nothing). 현재 롤업은 **전역 단일 `AMX_LANGFUSE_TENANT_ID`**
  에 귀속된다 — 스위퍼는 이 테넌트의 계정만 순회·적재하고, REST(`GET /tenants/{id}/usage/langfuse`,
  TenantScope)는 이 테넌트 행만 반환하므로 다른 테넌트 조회는 빈 결과다. 응답에 `lastSyncedAt`
  (해당 테넌트 롤업 `max(updated_at)`, 없으면 null)로 신선도를 노출한다. PoC 대상 단일 프로젝트라
  전역 바인딩으로 시작하며, 테넌트별 프로젝트 매핑은 후속 과제다.
- **토큰 수집(usageByType)**: 토큰은 `usageByType` measure를 `usageType` dimension과 교차해 뽑아
  각 토큰 종류를 제 컬럼에 실측 적재한다(실측 확인). `usageType` 매핑은 `input`→`input_tokens`,
  `output`→`output_tokens`, `cache_read_input_tokens`→`cache_read_tokens`,
  `cache_creation_input_tokens`→`cache_creation_tokens`, `total`→`total_tokens`이며, 모르는
  값은 경고 로그만 남기고 무시한다. `usageType`이 교차 dimension이라 각 축은 (그룹×usageType) 행을
  반환하므로 스위퍼가 그룹별로 재조립한다. `count`는 한 그룹의 usageType 행마다 동일하게 반복되어
  `observation_count`는 `total` 행에서만 취한다(이중 계산 방지). 이전 `inputTokens` measure는
  input+cache_read+cache_creation 합산값이라 `input_tokens` 컬럼이 부풀려졌고 캐시는 0이었으나,
  이제 `input_tokens`는 순수 input, 캐시 두 컬럼은 실측치로 채워진다.

#### 5.6.2 경보 웹훅 발송 + Langfuse 임계값 경보 (P5, BACKLOG G41)

경보를 콘솔 밖으로 내보내는 범용 웹훅 계층과, Langfuse 실측치를 임계값으로 감시하는
경보 3종을 더한다.

- **아웃박스 패턴**: `services.alerts`의 open/resolve 프리미티브는 경보를 여닫는 것과
  **같은 트랜잭션**에서 `alert_webhook_outbox`(신규)에 전이 행을 스테이징한다(caller가
  커밋). 커밋이 롤백되면 아웃박스 행도 함께 사라지므로, 실제로 열리지 않은 경보의 유령
  웹훅이 나가지 않는다. `open_alert`은 `INSERT ... ON CONFLICT`의 `RETURNING (xmax = 0)`로
  진짜 신규 open만 골라 스테이징하고(이미 열린 경보의 refresh·동시 리포트 경합은 제외),
  resolve 계열은 실제로 닫힌 행만 `RETURNING`으로 골라 스테이징한다. 기존 8종·신규 3종
  모두 이 프리미티브를 통과하므로 별도 배선 없이 전량이 웹훅을 탄다.
- **전용 드레인 태스크**: `services.alert_webhook`가 전용 락 **`0x414D580F08`(…08)**로
  아웃박스를 드레인한다. offline 스위퍼 공유 루프가 **아니라** 서버 기동 시 뜨는 독립
  asyncio 태스크(`_alert_webhook_drainer`, 자체 주기 `AMX_ALERT_WEBHOOK_DRAIN_SECONDS`
  기본 30·최소 5)로 돈다 — 불량 수신자의 느린 POST가 오프라인 탐지·명령 복구를 밀어내지
  못하게 하는 것이 목적이다. HTTP POST는 P4 교훈대로 락·트랜잭션 밖에서 한다: 만기 행을
  짧은 트랜잭션에서 **고유 리스 토큰**으로 예약(`lease_token` 세팅 + `next_attempt_at`
  앞당김)하고 커밋해 락을 놓은 뒤 POST하고, 결과를 다시 짧게 반영한다. finalize는 리스
  토큰이 자신이 부여한 값과 일치하는 행만 삭제/백오프해(소유 검증) 리스 만료 후 재예약된
  행의 이중 처리를 막는다. 성공 → 행 삭제, 실패 → `attempt`+지수 백오프, 상한 초과 →
  경고 로그 후 폐기 + 관측용 셀프 경보 `alert_webhook_dropped` open(그 경보는 웹훅
  아웃박스에 싣지 않는다 — 재귀 방지). 배치는 20, POST 타임아웃은 http_timeout과 분리된
  `AMX_ALERT_WEBHOOK_TIMEOUT_SECONDS`(기본 5). 페이로드는 `{alertId, kind,
  status(open|resolved), tenantId, serverId, detail, occurredAt}`이고, 헤더
  `X-AMS-Timestamp`(유닉스 초)와 `X-AMS-Signature: sha256=HMAC-SHA256(시크릿, 타임스탬프
  +본문)`으로 무결성·리플레이를 막는다. **전달 의미론은 at-least-once·순서 미보장** —
  수신자는 `(alertId, status, occurredAt)`로 멱등 처리한다. 활성화는
  `AMX_ALERT_WEBHOOK_URL`/`AMX_ALERT_WEBHOOK_SECRET` 둘 다 설정된 경우에 한하며
  (all-or-nothing), 하나라도 없으면 스테이징 자체를 건너뛰어 완전 무부작용이다. 시크릿은
  서명 계산에만 쓰이고 로그에 남기지 않는다.
- **수신자 검증(pseudo)**: 수신 측은 본문을 원문 그대로 받아 서명을 재계산한다 —
  `expected = "sha256=" + hmac_sha256(secret, request.header["X-AMS-Timestamp"] + raw_body)`
  를 계산해 `X-AMS-Signature`와 상수시간 비교하고, `X-AMS-Timestamp`가 허용 시차(예: ±5분)
  안인지 확인해 리플레이를 거른다. 본문 재직렬화가 아니라 **받은 바이트 그대로** 서명해야
  일치한다(발신 측은 정렬·compact JSON을 서명·전송한다).
- **임계값 스위퍼**: `services.langfuse_alerts`가 전용 락 **`0x414D580F09`(…09)**로 offline
  스위퍼 공유 루프에 sibling으로 붙어(langfuse-metrics 다음 여덟 번째 sibling) Langfuse 활성
  게이트와 폴 주기(`AMX_LANGFUSE_POLL_SECONDS`)를 공유하되 독립 캐던스 상태로 경보 3종을
  open/resolve 한다(전부 시스템 범위, `server_id` NULL·테넌트 범위 dedupe). latency의 HTTP
  GET만 락 밖에서 먼저 하고, 나머지는 짧은 락+커밋으로 전이를 반영한다.
  - `langfuse_usage_spike` — 당일(UTC) 총 토큰이 전일 대비 `AMX_ALERT_SPIKE_FACTOR`(기본
    3.0) 배수를 초과하면 open. 전일이 0이면 배수가 무의미하므로 절대 하한
    `AMX_ALERT_SPIKE_MIN_TOKENS`(기본 1,000,000)를 초과할 때만 open, 복귀 시 resolve.
  - `langfuse_stale` — **metrics 스윕의 마지막 정상 시각 마커**(billing_cursors kind
    `langfuse_metrics_sync`, 스윕이 HTTP 왕복 성공 시 활동 유무 무관하게 상향)가
    `AMX_ALERT_STALE_MINUTES`(기본 60)를 넘겨 늙으면 open, 스윕 재개 시 resolve. 롤업
    `max(updated_at)` 대신 이 마커를 써 무활동 주말 오발을 피하고 파이프라인 정체만 잡는다.
    한 번도 정상 스윕이 없으면(마커 NULL) 평가하지 않는다.
  - `langfuse_latency` — Metrics API latency p95(measure `latency`, aggregation `p95`, 최근
    1시간)가 `AMX_ALERT_LATENCY_P95_MS`(기본 60000)를 초과하면 open, 이하 복귀 시 resolve.
    Langfuse latency는 초 단위라 ms로 환산해 비교하며, HTTP 오류·무데이터는 경고 후 스킵해
    경보 오발을 막는다.

#### 5.6.3 위험명령 경보 (P5, 경로 d)

러너에서 실행되려는 위험한 Bash 명령을 경보로 올린다. Langfuse는 툴 입력을 API로 노출하지
않아(v4 events_only) 이 경로로는 잡을 수 없으므로, Claude Code **PreToolUse 경량 훅**
(`deploy/langfuse/danger_hook.py`)이 명령을 직접 검사해 AMS로 통보하는 방식을 택했다.

- **훅**: Bash 실행 직전 stdin으로 받은 payload의 `tool_input.command`를 보수적인 위험
  패턴 목록과 대조한다. 감지·통보 전용이라 **차단하지 않고 항상 `exit 0`**으로 끝나므로
  Claude 동작은 불변이다. 통보 실패도 조용히 삼켜(2초 타임아웃) 세션을 느리게/실패하게
  하지 않는다. 통보 POST는 데몬 스레드+`join(2초)`로 감싸 DNS 지연까지 포함해 하드
  2초로 경계한다(스레드는 데몬이라 방치돼도 무해). `AMX_DANGER_INGEST_URL`/
  `AMX_DANGER_INGEST_TOKEN`이 없으면 즉시 무동작. vendored `langfuse_hook.py`와 무관한
  자체 파일이며 표준 라이브러리만 쓴다.
- **패턴 판정**: `rm` 재귀+강제 삭제는 정규식이 아니라 **선형 시간 토큰 검사**로 잡는다 —
  명령을 공백 분할해 `rm` 뒤 `-` 시작 플래그에서 r·f 동시 포함(또는 `--recursive`·
  `--force`)을 본다. 정규식 접근은 ReDoS(`rm -x -x …` 백트래킹)와 `rm -i`류 오탐을
  낳아 폐기했다. 나머지 패턴(sudo·mkfs·dd of=/dev·chmod -R 777·curl|sh·force push)은
  선형 룩어헤드 정규식이다. `_match` 진입 전 명령이 8KB를 넘으면 앞 8KB만 검사하고
  payload에 `truncated` 플래그를 싣는다(sha256은 원문 전체로 계산).
- **원문 비전송**: 페이로드는 `{patternName, commandSha256, commandMasked(패턴 매치
  키워드만 남기고 나머지 마스킹, 200자 이내), truncated, sessionId, cwd, hostname,
  userId?, ts}`. 원문 명령은 전송·저장 어디에도 남지 않는다. 정규식/토큰 검사 모두 셸을
  완전히 파싱하지는 않으므로 인용 문자열 오탐·난독화 누락을 완전히는 못 막는다(조기경보이지
  방어선이 아님).
- **수신**: `POST /api/v1/ingest/danger-command`. 무인 훅 호출이라 TenantScope가 아니라
  정적 토큰(`X-AMX-Ingest-Token` == `AMX_DANGER_INGEST_TOKEN`)만으로 인증한다. 토큰 또는
  귀속 테넌트 미설정 시 404로 비활성, 오토큰 401. 무자격 도달 경로라 본문 파싱 전
  Content-Length 상한(64KB, 초과 413)으로 값싸게 거른다. 폭주 방어로 전역 고정창 레이트
  제한(`AMX_DANGER_RATE_LIMIT_PER_MIN` 기본 120, 초과 429·로그).
- **경보 kind `dangerous_command`**(alembic 0023): 서버에 매이지 않는 시스템 범위
  (`server_id` NULL)이되 **실 테넌트**(`AMX_DANGER_TENANT_ID`, 없으면 `AMX_LANGFUSE_TENANT_ID`
  폴백)에 귀속시켜 콘솔 경보 목록·ack 동선에 정상 노출한다(langfuse 시스템 경보 관례와
  정렬). dedupe는 `(tenant, hostname, patternName, commandSha256)` 기반이라 같은 호스트의
  같은 명령 반복은 새 경보를 만들지 않고 detail만 갱신한다. **auto-resolve 없음**(이벤트성 —
  관리자가 ack/resolve). §5.6.2 웹훅 프리미티브(`open_event_alert`)를 통과하므로 아웃박스에도
  스테이징돼 웹훅으로 함께 나간다(알림이 목적). detail에는 마스킹본·해시·세션·호스트만
  담고 원문은 저장하지 않는다.

#### 5.6.4 관리 감사 로그 (콘솔 테스트 갭 G53)

관리자가 변경성 REST를 호출하면 응답 직후 `admin_audit_logs`에 한 행을 남긴다. 콘솔 테스트에서
"누가 언제 무엇을 바꿨는가"를 되짚을 수단이 없다는 갭이 드러나 넣은 장치다. 기록은 미들웨어
(`app/api/audit.py`)가 맡고, 라우터·서비스는 손대지 않는다.

- **대상과 제외**: POST·PATCH·PUT·DELETE만 남기고 GET류 조회는 남기지 않는다. 세 경로는 뺀다 —
  비밀번호가 실리는 `/auth/login`, 정적 토큰으로 인증하는 무인 에이전트 발 `/ingest/danger-command`,
  무인증 헬스체크 `/healthz`. 실패한 요청도 그 상태 코드와 함께 남긴다. 4xx·5xx가 오히려 감사
  가치가 높기 때문이다.
- **한 행에 담는 것**: `tenant_id`(전역 액션은 NULL), `admin_email`(세션 principal에서 —
  루트 토큰은 `<root-token>` 센티널), `method`, `path`(실제 경로), `action`(매칭된 라우트
  템플릿 `"{METHOD} {route path}"`), `target_id`(경로 끝 UUID 세그먼트, 있으면), `status_code`,
  `created_at`. **요청 바디는 저장하지 않는다** — 자격증명 세트(`POST …/accounts`)와 인가 코드
  (`:oauth-complete`)가 그 안에 실려, §7이 금하는 시크릿 잔존을 만든다. 마스킹 저장이 필요해지면
  별도 설계로 다룬다.
- **테넌트 삭제와 무관하게 보존**: `tenant_id`는 FK를 걸지 않는다. 전역 액션(테넌트 생성·관리자
  CRUD)은 매달릴 테넌트가 없고, 감사 이력은 대상 테넌트가 지워진 뒤에도 남아야 한다. CASCADE로
  이력이 함께 사라지면 감사의 의미가 없다.
- **미인증 요청은 남기지 않는다**: principal이 잡히기 전에 401/403으로 끝난 익명 요청은 기록에서
  뺀다. 익명 변경성 시도를 모두 남기면 프로브 루프가 트레일을 무한히 부풀리는 벡터가 되고, 침입
  시도 추적은 감사 로그가 아니라 액세스 로그의 몫이다.
- **기본 무기한 보존(의도)**: 감사 로그는 청소 스윕을 두되 `AMX_AUDIT_RETENTION_DAYS` 기본값이
  `0`이라 기본은 무기한 남긴다. 스냅샷·배정 정리는 기본으로 오래된 행을 지우는데 감사만 그렇지
  않은 비대칭은 감사의 성격 때문이다 — 트레일의 가치가 곧 그 수명이다. 보존 상한이 필요하면
  `AMX_AUDIT_RETENTION_DAYS`를 양수로 두어 기한 경과 행을 배치 삭제한다(형제 스윕, 락 …0B).
- **기록 실패는 요청을 깨지 않는다**: 행 쓰기가 실패하면 경고만 남기고 넘어간다. 감사가 정상
  동작을 500으로 되돌리는 일은 없어야 한다.
- **조회**: `GET /api/v1/tenants/{tid}/audit-logs?from&to&limit&pageToken` (TenantScope). 최신순으로
  돌려주고 `[from, to)` 반열림 구간으로 `created_at`을 거른다. 해당 테넌트 행은 항상 포함하되,
  전역(`tenant_id` NULL) 행은 global-admin일 때만 함께 준다 — tenant-admin이 자기 테넌트와
  무관한 액션을 볼 이유가 없다.

### 5.7 Credential 역동기화 (O9 회전형 대응, **구현 완료** — p2b-cred-resync)

O9가 **회전형**으로 판별됨(§8): 계정이 서버에서 활성으로 돌면 그 서버의 Claude Code/tsamx가
refresh하며 refresh token을 회전 → AMS 보관본(`accounts.encrypted_secret`)이 무효화된다. O2 변경
(2026-08-14, recall=항상 purge)으로 회수 시 로컬 보존이 사라졌으므로, **같은 서버든 다른 서버든**
재배정은 모두 full deliver이고 AMS가 보낼 수 있는 것은 무효화된 구본뿐이라 그대로면 실패한다.
이를 자동화로 메우는 역동기화:

- **AMA 감지**: tsamx가 refresh를 수행하면 로컬 `.credentials.json`이 갱신된다. AMA store/reporter가
  credential fingerprint 변화(§tsamx `oauth.credential_fingerprint` — refresh token 해시 기반)를
  감지 → 갱신된 credential 세트를 매니페스트 KEK로 재암호화.
- **전송**: `AmaMessage`에 신규 `CredentialUpdate`(proto 변경, AmaMessage oneof 확장) — `{ams_account_id,
  EncryptedCredential(갱신본), server_credential}`. P2 보안 준수: AAD 바인딩(amsAccountId‖agentId),
  세션 서명·TLS. credential은 채널에만 실리고 로그·DB 평문 저장 금지(§7).
- **AMS 갱신**: 수신(`_apply_cred_update`) → observed_at 필수·미래 skew clamp → 테넌트 조회 → 소유권
  검증(이 서버에 active/inactive/quarantined 배정만; pending/recalling 배제) → key_id 일치 → sealed box
  개봉(AAD 로컬 재유도) → UTF-8 디코드 → **토큰 재료 검사**(로그아웃 껍데기 거부: 프로바이더별 토큰
  키가 있는데 값이 전부 공백이면 저장·observed_at 전진 없이 드롭 — 복구본이 나중에 올라올 수 있게)
  → `crypto.encrypt_secret` **초크포인트** 경유 재암호화(F2 봉투암호화 자동 준수).
- **경합·단조성**: 설계 초안의 `credential_version`(정수) 대신 **`accounts.credential_observed_at`
  (에이전트 관측 시각, 벽시계 단조 래칫)** 채택 — 리부트 후에도 카운터 없이 생존, 원자적 조건부
  UPDATE(WHERE observed_at 가드)로 F3 다중 인스턴스·중복·롤백 재생을 거부. NULL = 최초 무조건 수락.
  (2026-08-09 소급 확인·승인 — as-built 우선, 정수 version 컬럼 없음.)
- **관찰 경계**: AMA resyncer는 활성 계정의 `.credentials.json`만 관찰한다. 활성이었다가 회전 직후
  tick 전에 전환·recall된 계정의 회전은 놓칠 수 있음 — 아래 폴백으로 정합성은 유지되므로 수용. 또한
  fingerprint가 바뀌었더라도 토큰 재료가 없는 세트(로그아웃 껍데기)는 push하지 않고 베이스라인도
  그대로 둔다 — AMS 편 검사와 같은 판정이며, 양편 공백 정의(공백 또는 제어문자)를 일치시켜야 한다.
  드롭한 tick에는 에이전트가 `credential_unusable` 이벤트(`AccountEvent.KIND_CREDENTIAL_UNUSABLE`,
  계정은 `from`)를 올려 AMS가 계정 범위 경보를 연다. 엣지 트리거라 사고당 한 번만 올라가며 재료가
  돌아온 tick에 상태가 풀려 다음 사고를 다시 알린다. 다만 이 신호는 활성 계정이고 매니페스트 레코드가
  있어야 나온다. 비활성 계정이나 AMS가 배달한 적 없는 계정의 같은 상태는 잡지 못한다. 경보는 같은
  계정의 cred_update가 실제로 저장되는 시점에 닫힌다.
- **폴백**: 역동기화 실패·유실 시 재배정은 §5.5 재인증으로 폴백(현 동작 유지) — 자동화 최적화이지
  정합성 필수 경로는 아님.

구현: proto `CredentialUpdate`(AmaMessage 15) · AMA `internal/resync/` · AMS `_apply_cred_update` ·
마이그레이션 0005 · E2E `e2e/test_o9_resync_e2e.py`. 이월: 가용성 격리(encrypt 실패 시 스트림 유지) 패치 별도.

---

## 6. AMA 설계 (에이전트면, Go)

### 6.1 프로세스 구성

단일 Go 데몬(systemd service). 내부 컴포넌트:

| 컴포넌트 | 역할 |
|---|---|
| transport | AMS gRPC 다이얼, 지수백오프 재연결, 스트림 유지 |
| command | 명령 수신 → 서명 검증 → 멱등 처리(cmdId 처리로그) → tsamx 브리지 호출 → 수렴 상태 ack |
| store | 암호화 매니페스트 (AMS 권위 할당표) 관리 |
| tsamx 브리지 | tsamx CLI exec + `--json` 파싱 + `autoswitch_state.json` fsnotify 감시 |
| reporter | 5분 폴링 리포트, 즉시 이벤트 push, 오프라인 아웃박스(재연결 시 dedupe 플러시) |
| scheduler | `tsamx auto --once` 틱 구동 (D8), 리포트 주기 관리 |

**설치 전제**: AMA installer가 내재화 tsamx 패키지 설치까지 수행 (D11):
`uv tool install "git+<AMX 레포 주소>@<태그>#subdirectory=tsamx"` — 운영 배포는 태그 핀 필수.
tsamx는 자체 버전으로 핀 관리하며, 업스트림(claude-swap) 반영은 O6 절차를 따른다.

### 6.2 로컬 어카운트 스토어 (요구 AMA-1, 4)

- **매니페스트 파일 1개** (암호화 JSON): AMS 권위 할당표 + **로컬 권위 credential 사본**.
  - 내용: `amsAccountId ↔ email` 매핑, 할당 상태(active/inactive), **암호화된 credential 세트 레코드**
    (재주입·정합 동기화의 재료), AMS 서명, 수신 시각.
  - tsamx의 `sequence.json`/`.creds-*`는 tsamx 소유의 **파생 사본**으로 취급한다 —
    Linux에서 base64뿐이라(§2.2) 실질 at-rest 보호는 매니페스트가 담당하고,
    파생 사본이 훼손·유실되면 매니페스트에서 재주입한다(아래 정합 동기화).
- **암호화**: 레코드별 **AES-256-GCM**, 유니크 nonce,
  AAD = `(amsAccountId + agentId)` 바인딩(다른 에이전트로 레코드 복사·스왑 차단).
  키(KEK)는 AMS가 세션 수립 시 전달하고 **메모리에만 보관** → 오프박스 파일 사본은 복호 불가,
  무인 재부팅 시 AMS 재연결 없이는 로컬 계정 정보를 열 수 없다("AMS 없이는 변경 불가" 부합).
  (대안 TPM/systemd-cred 봉인은 O1에서 **미채택** — 메모리 전용 확정, §8)
  KEK 전달은 **에이전트별 ephemeral X25519 sealed box 봉인**(C2): AMA가 연결마다 X25519 키쌍을
  생성해 공개키를 `Register.agent_public_key`에 싣고, AMS는 그 공개키로 세션 KEK를 NaCl sealed box
  봉인해 `SessionSetup.WrappedKey`로 전달한다. TLS를 종단하는 앞단(로드밸런서 등)에서도 KEK가
  평문으로 노출되지 않는다. 개인키는 세션 스코프 메모리 전용(재연결마다 교체). 공개키를 제시하지
  않는 세션은 거부되며, `AMX_ALLOW_RAW_KEK`(dev 전용) 원시 폴백은 프로덕션에서 사용 금지.
- **권위 강제는 암호화가 아니라 서명**: 매니페스트와 모든 명령에 AMS Ed25519 서명,
  AMA는 빌드에 내장된 공개키로 검증 후에만 적용. 위조 매니페스트는 검증 실패.
- **정합 동기화 (등록분만 사용 강제)**: 매 리포트 틱마다 `tsamx list --json`과 매니페스트를 대조.
  - tsamx에는 있는데 매니페스트에 없음 → `tsamx remove` (미등록 계정 사용 차단)
  - 매니페스트에는 있는데 tsamx에 없음 → 보관된 credential 세트를 파일 기록 + `tsamx add` 재주입 (§6.3 deliver와 동일 절차)
- **현실 한계 (명시)**: root 사용자의 자기 서버에서 완전한 변조 방지는 불가능
  (메모리 덤프, 바이너리 패치). 실질 보장선은 §3 신뢰 모델 + AMS reconcile 드리프트 경보.

### 6.3 명령 처리 (요구 AMA-4) — 전부 멱등

| AMS 명령 | 로컬 절차 | 재전송 시 |
|---|---|---|
| deliver | 서명 검증 → credential 세트 복호 → 매니페스트 upsert → 이전 활성 계정 기록 → credential 파일 기록(`~/.claude/.credentials.json` + `~/.claude.json` oauthAccount) → `tsamx add` (슬롯 자동 할당) → 필요 시 `tsamx switch <이전 활성>` 복귀 → 평문 메모리 소거 | 이미 존재하면 no-op, 수렴 상태 회신 |
| recall | 대상이 활성이면 먼저 `tsamx switch <타계정>` → **항상 `purge_local_copy=true`(O2 변경 2026-08-14)**: `tsamx remove` + 매니페스트 레코드 삭제. provider 무관(claude·codex 동일) AMS가 항상 `true`로 발행 — 회수는 해당 서버에서 완전 분리이고 이력은 detached 행·이벤트로만 남는다 | 부재 시 성공 no-op |
| activate | `tsamx enable` + 매니페스트 상태 갱신 | 동일 상태면 no-op |
| deactivate | `tsamx disable` (크레덴셜 유지, 로테이션만 제외) | no-op |
| switch_now | `tsamx switch <num\|email>` (또는 `--strategy best`) | 이미 활성이면 no-op |
| set_mode | auto: scheduler 틱 시작 / manual: 틱 중지 | 값 동일 no-op |
| req_report | 즉시 §6.5 리포트 생성·전송 | 항상 수행 (조회는 멱등) |
| self_update | 프리플라이트(go 툴체인·디스크) → `git fetch` → `git rev-parse @{u}`로 원격 tip 확인 → (핀 지정 시) tip 대조 → `git pull --ff-only` → `go build -o ama.new ./cmd/ama` → `ama.new --version` 스모크 → 현 바이너리를 `ama.bak`으로 백업 → `rename(ama.new → ama)` → applied.log 기록 → ack → `execve` 재기동 | applied.log에 CONVERGED면 재빌드·재기동 없이 CONVERGED 재회신 |

- ⚠ **self_update는 소스를 명령으로 받지 않는다**: 프로토 `SelfUpdate`에는 `expected_commit`
  하나뿐이고 저장소 URL·브랜치·빌드 플래그 필드가 없다. 에이전트는 운영자가 심어둔 자기 클론의
  upstream만 `--ff-only`로 당기고 자기 `./cmd/ama`만 빌드한다. AMS가 털려도 공격자 소스를 가리킬
  수는 없다는 것이 이 명령 설계의 전부이므로, 나중에도 소스 지정 필드를 추가하지 않는다.
- ⚠ **핀 대조는 pull 앞에서 한다**: `expected_commit`은 "이 커밋이 되어라"가 아니라 "이 커밋이
  아니면 하지 마라"는 거부권이다. 그래서 fetch로 추적 ref만 갱신한 뒤 `@{u}`(현재 브랜치의 upstream)
  tip과 먼저 대조하고, 일치할 때만 pull한다. 대조를 pull 뒤로 미루면 운영자가 명시적으로 거부한
  커밋으로 작업 트리가 이미 이동한 뒤가 된다. 불일치면 `commit_mismatch`로 nack하고 트리는 그대로다.
  짧은 해시(7자 이상) prefix 매칭이며 대소문자는 무시한다. pull 뒤 HEAD를 한 번 더 보는 것은
  이중 방어일 뿐 1차 관문이 아니다.
- ⚠ **실패는 전부 무변경으로 끝난다**: fetch·핀 대조·pull·빌드·스모크는 **엔진 락 밖**에서 돌고
  설치된 바이너리를 건드리지 않는다. 어느 단계에서 깨지든 DIVERGED(`preflight_failed`/
  `git_fetch_failed`/`no_upstream`/`commit_mismatch`/`git_pull_failed`/`build_failed`/`smoke_failed`)로
  nack하고 에이전트는 쓰던 바이너리로 계속 돈다. 각 단계에는 상한이 걸려 있고(git 120s·빌드 600s·
  스모크 15s) 초과하면 `timeout_git`/`timeout_build`/`timeout_smoke`다 — 응답 없는 remote나 물린
  링커가 명령을 영원히 붙잡고 있으면, 교체 전까지 락을 잡지 않는 설계 탓에 운영자 눈에는 그냥 ack이
  오지 않는 에이전트로만 보인다. 검증된 바이너리가 나온 뒤에야 엔진 락을 잡고 백업·교체·기록·
  ack·exec을 잇달아 수행한다.
- ⚠ **스모크는 세 조건을 모두 요구한다**: 15초 내 종료, exit 0, 그리고 출력에 방금 빌드한 커밋
  해시가 들어 있을 것. 셋째가 핵심이다 — `--version`을 모르는 커밋(기능 revert, 구 브랜치)은 그
  플래그를 그냥 무시하고 **정식 에이전트로 기동해** 버린다. 그대로 설치하면 중복 등록이고, 그
  바이너리는 다시는 self_update를 받을 수 없다. 스모크 실행 env는 `PATH`·`HOME`만 넘기는
  화이트리스트라 `AMX_AMS_ADDR`·`AMX_AGENT_ID`가 없고, 그런 빌드는 AMS를 못 찾아 상한에 걸려
  죽거나 커밋 해시 없는 출력을 내므로 어느 쪽이든 거부된다.
- ⚠ **ack은 "교체 성공"이지 "신버전 기동"이 아니다**: CONVERGED는 디스크의 바이너리를 바꾸고
  재기동을 요청했다는 뜻까지다. 새 버전이 실제로 떴는지는 다음 Register의 `agent_version`
  (`p3+<shortsha>`)으로만 확인한다. ack 송신은 exec 전에 전송 확인까지 최대 2초 기다리지만
  best-effort다 — 유실돼도 AMS가 같은 command_id를 재큐하고 재기동한 프로세스가 applied.log를 보고
  CONVERGED를 재회신한다. 그래서 applied.log 기록이 exec보다 **먼저**여야 한다. 기록이 날아가면
  재큐마다 다시 빌드하고 다시 재기동하는 부트 루프가 된다. exec 자체가 실패한 경우에도 이미 확정한
  CONVERGED를 뒤집지 않는다 — 교체는 성공했고 다음 재시작에 반영되므로, DIVERGED로 정정하면
  사실과 어긋나는 데다 재큐 억제까지 풀린다.
- ⚠ **플릿 적용은 한 대씩 검증하고 넓힌다**: 1대에 먼저 걸고 `agent_version`이 새 커밋으로 바뀌는
  것을 확인한 뒤 나머지에 건다. 한 서버에 동시에 두 건은 못 건다 — queued/sent인 self_update가
  있으면 REST가 409 `self_update_already_pending`으로 막는다(버튼 연타·중복 스크립트 방지).
- ⚠ **버전 스큐**: 핀 없이 보내면 에이전트는 자기 upstream tip으로 간다. 그 tip이 지금 돌고 있는
  AMS보다 앞설 수 있고, 그러면 서버가 모르는 계약으로 빌드된 에이전트가 뜬다. 서버를 먼저 올리거나
  `expected_commit`으로 못을 박아라.
- ⚠ **로컬 클론을 신뢰한다는 전제**: 이 설계는 AMS를 신뢰하지 않는 대신 에이전트의 클론과 그
  추적 브랜치를 신뢰한다. 따라서 **그 브랜치에 push할 수 있는 사람은 에이전트 호스트에서 코드를
  실행할 수 있는 사람과 같다.** 배포용 추적 브랜치의 push 권한은 그 기준으로 관리한다.
- ⚠ **연결 단계에서 죽으면 수동 복구**: 새 바이너리가 뜨자마자 크래시하면 감시형 러너가 없어
  자동 롤백이 걸리지 않는다(승격은 보류 결정). 직전 바이너리는 `ama.bak`에 남아 있고, 정석 복구는
  `git -C ~/AMX reset --hard origin/main && bash deploy/agent-run.sh up`이다. 실패 ack은 서버에서
  `self_update_failed` 알림으로 뜬다 — 서버-scoped 명령이라 되돌릴 배정이 없어서, 알림이 아니면
  업데이트가 안 먹은 사실이 아무 데도 안 보인다.

- ⚠ **deliver 크리티컬 섹션 (B1 구현)**: `add`가 신규 슬롯을 활성으로 만들지만 deliver는 풀 추가·
  enable/disable일 뿐이므로(활성 전환은 §6.4 auto/switch_now 소유), AMA는 **add 전 활성 계정을 기록해
  add 후 복귀**한다 — deliver가 러너의 라이브 계정을 바꾸지 않는다. credential 파일은 **원자적 쓰기**
  (temp+rename)로 러너의 부분 읽기를 막는다. 이 둘(복귀+원자적)이 과금 창을 sub-second로 좁히는 **주 방어**다.
- ⚠ **flock 조율 (B1b, 보조 방어)**: 남는 sub-second 창은 `.claude`의 lock 파일 flock으로 러너 기동과
  조율한다 — AMA는 크리티컬 섹션 동안 `LOCK_EX`를, 러너 래퍼(`deploy/amx-claude`)는 `LOCK_SH`를 잡아
  겹치지 않게 한다. **불변식**: flock 획득은 **엔진 락 밖에서 논블로킹(`LOCK_NB`)+상한(기본 5s)**으로
  하고, 초과 시 flock 없이 진행(위 주 방어로 폴백) — 러너 대기가 **엔진 락을 점유하지 않아** 스케줄러
  틱·다른 명령이 정지하지 않는다(§6.4 무인 운영 유지). 래퍼 미경유 직접 실행 경로는 협조적 advisory
  락이라 미커버(주 방어의 sub-second 창만 남음) — 배포 경계는 O5·`docs/DEPLOYMENT-RUNNER.md`.
- ack는 단순 성공/실패가 아니라 **수렴 상태**(현재 로컬 실상)를 회신 → AMS reconcile 입력.
- **AMS 연결 두절 시**: 현행 로스터로 스위칭 엔진 **계속 가동**(무인 운영 유지),
  로컬발 계정 변경은 거부, 이벤트는 암호화 아웃박스에 큐잉 → 재연결 시 dedupe 플러시.
- **멱등 처리로그와 콜드스타트 (O1 메모리 KEK 파급 — 3규칙)**: `command_id` 처리로그는
  **평문 사이드카**(`applied.log`)에 저장한다 — command_id는 비밀이 아니며, 재부팅 후
  KEK 없이도 읽혀야 하므로 암호화 매니페스트와 분리한다. 재전송 no-op 판정은
  `command_id ∈ applied.log` **AND 현재 실상이 desired와 일치**를 모두 만족할 때만 —
  재부팅으로 실상이 소실됐으면 로그에 있어도 재실행한다. 이로부터:
  1. `SessionSetup`(KEK 전달)은 `applied_command_ids` 억제 대상에서 **영구 제외** —
     AMS는 매 세션 시작 시 Register 직후 무조건 하달한다(재부팅 교착 방지).
  2. 콜드스타트 시 KEK 미보유로 `Register.accounts`가 빌 수 있다. AMS는 **빈 Register로
     삭제형 reconcile을 하지 않고**, SessionSetup 이후 첫 `UsageReport`를 실상 권위로 삼는다.
  3. AMS의 deliver 재하달 억제는 `command_id ∈ applied_command_ids` **AND** 보고된 actual에
     해당 계정이 존재할 때만 — 재부팅 후 actual이 비면 억제가 풀려 재하달이 진행된다.

### 6.4 자동 스위칭 (요구 AMA-3, D8)

```
scheduler 틱 (적응 주기, 기본 60s)
  → tsamx status --json      (틱 전 활성 계정 기록)
  → tsamx auto --once        (tsamx 엔진이 95% 판정·스위칭·격리 수행)
  → tsamx status --json      (틱 후 비교)
  → 활성 계정 변경 감지 시   → AccountEvent(switch) 즉시 AMS 전송
  + autoswitch_state.json fsnotify 감시 (이중 감지: lastSwitchAt/lastSwitchTo, quarantine)
```

- **전체 스위칭 정책은 AMS가 `SetPolicy`(cmd 17)로 중앙 하달 (O4-B, F4)**: threshold·default_strategy·cooldown·hysteresis 4축 모두 → AMA가 `tsamx config set autoswitch.{threshold,cooldownSeconds,hysteresisPct}` 주입(키는 tsamx json_key=camelCase). 세션마다 재천명, 음수=unset·0=실제값(threshold는 0=unset). quarantine·후보 랭킹 엔진 로직은 tsamx 그대로(재구현 없음).
- **전 계정 소진**(all-exhausted, 전부 임계↑) 감지 시 → 크리티컬 이벤트 전송
  → AMS가 경보 + 추가 계정 배정 판단.

**P3 설계 확정 (설계노트 `docs/design-notes/p3-architecture.md` 참조):**
- **세션 권위 재천명**: AMA의 `switch_mode`·정책은 메모리 전용(재부팅 소실)이므로, AMS는 매 세션 시작 시
  `SessionSetup → SetSwitchMode → SetPolicy`를 **무조건 재천명**한다(applied 게이트 제외). 재천명 없으면
  재시작한 에이전트가 zero-value=MANUAL로 떨어져 자동 스위칭이 멈춘다.
- **reconcile-on-report**: reconcile은 별도 타이머가 아니라 **UsageReport 케이던스로 구동**한다. 5분마다
  도착하는 리포트가 actual 권위이므로 수신 시점에 desired(assignments) vs actual을 대조한다. 자동 교정은
  안전·멱등 케이스로 좁게 게이트하고 루프 방지 카운터를 둔다.
- **엔진 락**: scheduler 틱(`auto --once`)과 command 핸들러(deliver 크리티컬 섹션)는 같은 tsamx 풀을
  만지므로, 모든 bridge 변경 시퀀스를 단일 mutex로 직렬화한다(P3 최대 동시성 난제).
  **불변식(B1b)**: deliver의 러너 flock 대기(외부 프로세스 대기)는 **엔진 락을 점유하지 않는다** —
  flock은 엔진 락 밖에서 논블로킹+상한으로 획득하므로, 장수명 러너가 있어도 틱·명령이 정지하지 않는다.

### 6.5 보고 스키마 (요구 AMA-2) — 5분 폴링 + 수동 조회 공용

AMA는 usage API를 직접 폴링하지 않는다 — tsamx 캐시(`list --json`)를 재직렬화만 한다
(이중 폴링·레이트리밋 회피).

```jsonc
// UsageReport
{
  "schemaVersion": 1,
  "reportType": "usage",              // usage | switch_event
  "agentId": "ama_...",
  "generatedAt": "2026-08-07T09:00:00Z",
  "trigger": "schedule",              // schedule | ams_query | switch
  "activeAccount": { "amsAccountId": "acc_3", "email": "a@x.io" },
  "poolSummary": {
    "total": 5, "active": 1, "eligible": 4, "quarantined": 0,
    "allExhausted": false, "maxUtilizationPct": 61.2
  },
  "accounts": [
    {
      "amsAccountId": "acc_3",
      "email": "a@x.io",
      "allocationStatus": "active",   // AMS 할당 상태
      "isCurrent": true,              // 현재 활성 크레덴셜 여부
      "usage": {
        "fiveHour": { "pct": 61.2, "resetsAt": "2026-08-07T12:30:00Z" },
        "sevenDay": { "pct": 44.0, "resetsAt": "2026-08-11T00:00:00Z" },
        // 이하 P2b/P4 확장 (선택 필드, tsamx가 보고할 때만 실림):
        "windows": [                     // 프로바이더 일반화 윈도우(P2b) — fiveHour/sevenDay와 병기, windowMinutes 오름차순
          { "id": "five_hour", "windowMinutes": 300, "pct": 61.2, "resetsAt": "2026-08-07T12:30:00Z" },
          { "id": "seven_day", "windowMinutes": 10080, "pct": 44.0, "resetsAt": "2026-08-11T00:00:00Z" }
        ],
        "spend": {                       // 종량제 지출(tsamx spend) — 정보용, 스위칭·poolSummary 계산에 미반영
          "used": 12.4, "limit": 100.0, "pct": 12.4, "currency": "USD"
        },
        "scopedWindows": [               // 모델별 주간 윈도우(tsamx scoped) — 모델명 키, 스위칭·poolSummary에 미반영
          { "model": "claude-sonnet-4", "pct": 30.5, "resetsAt": "2026-08-11T00:00:00Z" }
        ]
      },
      "usageFetchedAt": "2026-08-07T08:59:48Z"
    }
  ]
}
```

`windows`/`spend`/`scopedWindows`는 #69에서 계약(스키마·proto)까지 관통해 수집·적재되는
선택 필드다. `spend`(종량제 월 상한 대비 지출)와 `scopedWindows`(모델별 주간 한도)는 **정보용**이라
스위칭 결정이나 `poolSummary.maxUtilizationPct`에 절대 들어가지 않는다(스위칭은 여전히
`fiveHour`/`sevenDay` binding-window 최댓값만 본다, §2.2). 콘솔 표시는 P4에서 미구현이며,
현재 관측 뷰는 §5.6.1 Langfuse 롤업이 담당한다.

```jsonc
// AccountEvent (스위칭 즉시 통지)
{
  "schemaVersion": 1,
  "reportType": "switch_event",
  "agentId": "ama_...",
  "eventId": "evt_...",               // 아웃박스 dedupe 키
  "occurredAt": "2026-08-07T09:12:00Z",
  "event": {
    "kind": "switch",                 // switch | quarantine | all_exhausted
    "trigger": "at-limit",            // at-limit | manual | failover
    "from": { "email": "b@x.io" },
    "to":   { "email": "a@x.io" }
  },
  "poolSummary": { "allExhausted": false, "maxUtilizationPct": 95.3 }
}
```

---

## 7. 보안 설계

| 계층 | 설계 |
|---|---|
| At-rest (AMS) | **봉투암호화 (P5 F2)**: `accounts.encrypted_secret`을 **테넌트별 DEK**로 AES-256-GCM(AAD=tenant_id, `v2:` 태그) 암호화, DEK는 **KEK provider**로 래핑해 `tenant_deks`에 저장(`AMX_KEK_PROVIDER=local\|aws-kms\|vault`; local MVP는 `AMX_KEK` env, KMS는 어댑터 자리). 모든 at-rest 접근이 단일 초크포인트(`crypto.encrypt_secret`/`decrypt_secret`) 경유 → O9 역동기화 포함 전 경로 자동 DEK 경유(이중암호 붕괴 구조적 방지). 인증용 시크릿과 **분리**. **전환**: 레거시 Fernet(`AMX_ENCRYPTION_KEY`, 접두 없음) 읽기 병행, v2 쓰기는 `AMX_ENVELOPE_WRITE=1` 게이트(무중단·롤백 경계). **롤백 주의**: 플래그 off는 읽기 유지(안전)이나, **코드 롤백/`0008` downgrade는 v2→Fernet 역-rewrap(`rewrap_secrets.py --reverse`) 선행 필수** — 미실행 시 DEK 소실로 v2 credential 복호 불가. **KMS 정직성**: local MVP는 단일 env 시크릿이라 기밀 강화는 실 KMS 도입 시(격리·구조는 즉시 이득) |
| In-transit | gRPC TLS 필수(§B4 — one-way 기본, mTLS 옵션·defense-in-depth) + 앱 계층 Ed25519 명령 서명. **세션 KEK는 에이전트별 ephemeral X25519 sealed box로 봉인**(C2, §6.2) — TLS 종단 앞단에서도 KEK 평문 없음. credential 세트는 deliver/재주입 시에만 스트림에 실리고 채널·로그에 저장되지 않음. **토큰/credential/KEK는 절대 로깅 금지**. **위협 경계**: sealed box는 익명 봉투라 발신자 신원을 바인딩하지 않으므로 *선의의 종단 프록시*까지만 방어한다 — 능동 MITM이 `Register.agent_public_key`를 치환하는 공격은 TLS(특히 mTLS, §B4)가 막는다 |
| 등록 플로우 (§5.5) | PKCE verifier는 서버 세션 보관·**1회용**(교환 성공/실패 시 즉시 폐기). authorize 코드는 짧은 TTL 내 교환, 미사용 시 폐기. `:oauth-start`/`:oauth-complete`는 관리자 인증 + 테넌트 RBAC 필수 |
| At-rest (AMA) | AES-256-GCM 매니페스트 + AAD 바인딩 + KEK 메모리 보관 (§6.2) |
| AMA 인증 | 1회성 enroll-token(발급 시 해시만 DB 저장) → 최초 등록 시 장수명 server credential 교환 → 이후 세션은 credential 제시. credential은 서버측에서 tenant_id에 바인딩 |
| 테넌트 격리 | DB 복합 FK 불변식(§5.1) + 서비스 계층 검증 + 명령 하달 시 서버 등록 tenant로 필터(클라이언트 제공 tenant 불신) — 삼중 방어 |
| 변조 대응 | AMS reconcile(desired vs actual) + 드리프트 경보. 로컬 완전 방지는 불가함을 전제(§6.2) |
| REST 인증 | **다중 관리자 + 2-role RBAC (P5 F1)**: `admins` 테이블(bcrypt) + DB opaque 세션토큰(`admin_sessions`, TTL·해시저장·revoke). 역할 = `global-admin`(전 테넌트) / `tenant-admin`(자기 테넌트만). 스코프 집행은 **라우터 공통 의존성** `require_tenant_scope`(경로 `/tenants/{tid}` ↔ Principal 스코프) — 엔드포인트 누락 불가. 교차 테넌트 = **404 은닉**, 역량 거부 = 403(스코프 먼저→역량). **부트스트랩** = `AMX_ADMIN_TOKEN` Bearer(항상 global-admin, admins 무관 상시 유효 → 구조적 잠금 불가, M2M/break-glass). 첫 인간 관리자는 `admin_cli`. 격리는 스코프 dep(도달 제어) + 서비스층 tenant 재검증·§5.1 복합 FK(데이터 무결성) + 메타테스트(회귀 방어)로 중첩 |

---

## 8. 미해결 / 후속 결정 항목

> 아래 표는 설계 결정(O1~O10). 리뷰에서 이월된 구현·운영 항목까지 포함한 종합 백로그는
> **`docs/BACKLOG.md`** 참조 (배포·하드닝·회복 엣지·콘솔 갭·P5·nit 분류 + 우선순위).

| # | 항목 | 선택지 | 시점 |
|---|---|---|---|
| O1 | AMA KEK 보관 | **결정: 메모리 전용** (2026-08-08). 재부팅 시 KEK 소실 → AMS 재연결로 `SessionSetup` 재수신해야 로컬 스토어 복호. TPM/봉인 없음. 콜드스타트 3규칙은 §6.3 | ✅ 확정 |
| O2 | recall 시맨틱 | **변경: 항상 purge**(2026-08-14, 사용자 지시). 회수 = 해당 서버에서 계정 완전 분리 — provider 무관 항상 `purge_local_copy=true`(`tsamx remove`+레코드 삭제)이고, 이력은 detached 배정 행·이벤트로만 남으며 재배정은 재전달이다. §5.2·§6.3 반영. **이전 결정(2026-08-08)**: disable만(`false` 기본, 레코드 보존) — 그러나 회수 잔재가 usage 보고에 INACTIVE로 실려 reconcile 불일치·비용의 옛 서버 배분을 유발해 폐기. codex는 애초에 사이드카 문제(`codex_single_account`)로 항상 purge가 필수였고, 이제 claude도 동일 | ✅ 확정 |
| O3 | API-key 계정 | 구독 쿼터 없어 95% 임계 무의미 — 관리 대상 포함 여부. 포함 시 등록 경로는 `tsamx add-token`이 여전히 유효 (api_key는 대화형 로그인 불필요, §2.4-5의 폐기는 oauth 한정) | P1 중 |
| O4 | 스위칭 정책 소유권 | **O4-B 완성 (P5 F4)**. `threshold_pct`·`default_strategy`(O4-C, cmd 17 필드 1·2)에 더해 **`cooldown_seconds`·`hysteresis_pct`(F4, 필드 3·4)도 AMS가 `SetPolicy`로 중앙 하달** → 전체 스위칭 정책 중앙화. AMA가 `tsamx config set autoswitch.{cooldownSeconds,hysteresisPct}` 주입(음수=unset·0=실제값). §6.4·설계노트 P3/F4 | ✅ 확정 (O4-B) |
| O5 | 러너 config 공유 | **부분 해소(B1)**: deliver 오과금은 이전활성 복귀+원자적 쓰기(주 방어)+flock 조율(보조, §6.3)로 방어, `docs/DEPLOYMENT-RUNNER.md` 배포 가이드. 남은 것: 래퍼 미경유 직접 실행의 sub-second 창(배포 강제), 러너와 AMA의 `~/.claude` 공유 보장 | 부분 해소, 배포 강제 잔여 |
| O6 | tsamx 업스트림 동기화 절차 | claude-swap 업스트림 갱신을 `vendor/claude-swap-upstream` 3-way 비교로 수동 병합. CLI/JSON 호환성 검증 체크리스트 + 소유자 | P1 이후 운영 |
| O7 | 다중 AMS 인스턴스 | 세션 레지스트리 + 내부 라우팅 (P1은 단일 인스턴스로 미룸) | SaaS 단계 |
| O8 | ClickEye 연동 형태 | **P4에서 건너뜀** (2026-08-08, 사용자) — 계정 스위칭 관제에 집중. ClickEye가 AMS 조회 API를 읽는 방식·범위는 추후 결정. 권장 후보: 신규 read-only 엔드포인트 + ClickEye 전용 API 키 | 미해결 (P4 이후) |
| O9 | refresh token 회전 | **판별: 회전형 확정** (2026-08-08, 실계정 실험 `tools/o9_refresh_probe.py` — 1회 refresh 후 구 refresh token이 `invalid_grant`로 거부됨). **결정: credential 역동기화 채택** — AMA가 로컬 refresh로 갱신된 credential 세트를 AMS로 역전송해 `accounts.encrypted_secret`을 최신화, 크로스서버 재배정을 사람 개입 없이 자동화. 같은 서버 재배정은 O2(로컬 보존)로 이미 커버. **구현 완료**(§5.7 — proto `CredentialUpdate`, AMA resync, AMS `_apply_cred_update`, `credential_observed_at` 단조 래칫, E2E). 잔여: 가용성 격리 패치(진행 중) | ✅ 구현 완료 |
| O10 | tsamx 설치 인증 (D11 파급) | 프라이빗 레포 git 설치에 필요한 서버측 인증. 1차: 읽기 전용 deploy key 공용(만료 없음·최소 권한) → 서버 증가/조직 이전 시 서버별 키 또는 machine user → P2 이후 AMS 아티팩트 서빙(wheel)으로 GitHub 의존 제거 검토 | P2 배포 설계 |

---

## 9. 구현 로드맵 (Phase)

| Phase | 산출물 | 완료 판정 |
|---|---|---|
| **P0 계약** | `contracts/proto/amx.proto` + 보고 JSON 스키마 + REST openapi 초안, 코드 생성 파이프라인 | 3개 언어(go/py/ts) 생성 코드 컴파일 통과 |
| **P1 인벤토리** | DB 스키마 + REST CRUD(테넌트/계정/서버/배정) + **§5.5 중앙 OAuth 등록**(`:oauth-start`/`:oauth-complete`) + 격리 불변식 테스트, 통신 없음 | 교차 테넌트 배정 INSERT가 DB에서 거부되는 테스트 통과 + 실계정 1개를 OAuth 플로우로 등록해 `encrypted_secret` 복호 시 완전한 credential 세트 확인 (P2 E2E의 "계정 10개" 전제 충족 경로) |
| **P2 채널** | gRPC bidi 세션 + enroll → deliver/recall/activate/deactivate 왕복 + 사용량 인제스트 + AMA 암호화 스토어 | 계정 10개 → A:3/B:5/C:2 배정·하달·회수 E2E 통과 |
| **P3 스위칭 제어** | auto/manual 모드, switch-now, `auto --once` 틱, 스위칭/소진 이벤트, reconcile 루프 | 95% 도달 시 자동 스위칭 + AMS 이벤트 수신 E2E 통과 |
| **P4 콘솔·운영** | ams-web 대시보드(모니터링/CRUD), 경보, ClickEye 조회 연동 | 콘솔에서 전 수명주기 조작 가능 |
| **P5 SaaS 준비** | 테넌트 RBAC, 봉투암호화, 다중 인스턴스, 과금 훅 | (장기) |

---

## 부록 A. 요구사항 ↔ 설계 대응표

| 요구 | 설계 반영 위치 |
|---|---|
| AMS-1 테넌트 CRUD | §5.1 tenants, §5.3 |
| AMS-2 테넌트 내 어카운트 CRUD | §5.1 accounts, §5.3, §5.5 (중앙 OAuth 등록) |
| AMS-3 RDBMS 관리 | §5.1 (PostgreSQL) |
| AMS-4 테넌트:서버 1:N + 주입 CRUD | §5.1 servers/assignments, §5.3 |
| AMS-5 배정 하달 + 프로토콜 교체 가능 | §5.4 (gRPC bidi + AmaTransport 포트) |
| AMS-6 활성/비활성·회수/전달·자동/수동 스위칭 | §5.2 상태기계, §5.3 액션, §6.3 |
| AMS-7 테넌트 격리 (임의 연결 불가) | §5.1 복합 FK 불변식, §7 삼중 방어 |
| AMA-1 수신 계정 파일 관리 + 등록분만 사용 | §6.2 매니페스트 + 정합 동기화 |
| AMA-2 5분 폴링 + 수동 조회 보고 | §6.5, §5.3 refresh-usage |
| AMA-3 95% 자동 스위칭 (tsamx 활용) | §6.4, D8/D10 |
| AMA-4 AMS 명령만 수용 + 암호화 | §6.2 서명·암호화, §6.3, §3 신뢰 모델 |
