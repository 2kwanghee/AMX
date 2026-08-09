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
  payload JSONB,                       -- 보고 원문 보존
  reported_at TIMESTAMPTZ
)

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
  kind TEXT,                            -- usage_daily
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
| recalling | 회수 명령 전송, 확인 대기 | `tsamx disable` 수행 중 (credential 레코드 **보존**, O2 기본); `purge_local_copy=true`면 `tsamx remove` + 레코드 삭제 |
| detached | 종말 상태 (행은 감사용 유지) | 로테이션 제외 + credential 레코드 보존(빠른 재배정); `purge_local_copy=true` 시에만 로컬 흔적 완전 제거 |

- **스위칭 모드**는 서버 단위 속성(`servers.switch_mode`), 계정 단위 제외는 `pinned`로.
- **비상태 명령**: `switch_now`/`set_mode`/`req_report`는 배정 상태를 전이시키지 않는다
  (switch-now는 `last_switched_at`만 갱신, set_mode는 `servers.switch_mode`만 변경).
- **recover 전이**: REST `POST …/assignments/{id}:recover`(§5.3)가 트리거 →
  AMA에 `set_active(activate)` 하달 → ack 시 quarantined → active.
- **재배정 단축 (O2 파급)**: recall 기본이 credential 레코드를 보존하므로, 같은
  계정을 같은 서버에 다시 배정할 때 AMA에 보존 레코드가 있으면 deliver는 재주입 대신
  `tsamx enable`로 단축된다(credential 재전송 불필요). 레코드가 없거나
  `purge_local_copy=true`로 삭제됐던 경우에만 full deliver.
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
| `POST/GET /api/v1/tenants/{tid}/assignments` | 배정 생성·목록 (예: 10개 중 A:3 / B:5 / C:2) |
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
복구된다 — 멱등이라 유실은 없고 지연만.)

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
- **서버 오프라인 정합**: `last_seen_at` 만료 스위퍼(주기적 `< now-3틱 → offline`) — half-open 스트림에서
  online 고착·경보 미발화 방지.
- **인증**: ams-server는 단일 관리자 Bearer 유지, BFF에 로그인→쿠키 세션 + `Principal` 반환형(P5 테넌트
  RBAC 훅 자리)만 추가. 멀티테넌트 RBAC는 P5.
- **완료판정 검증**: BFF API 레벨(Route Handler 프로그램 구동)로 전 수명주기 조작 판정 + OAuth 등록
  마법사·deliver/recall만 실브라우저 Playwright 스모크.

### 5.7 Credential 역동기화 (O9 회전형 대응, **구현 완료** — p2b-cred-resync)

O9가 **회전형**으로 판별됨(§8): 계정이 서버에서 활성으로 돌면 그 서버의 Claude Code/tsamx가
refresh하며 refresh token을 회전 → AMS 보관본(`accounts.encrypted_secret`)이 무효화된다. 같은 서버
재배정은 O2(recall=disable, 로컬 credential 보존)로 커버되나, **다른 서버로 재배정**하면 AMS가 보낼
수 있는 것은 무효화된 구본뿐이라 deliver가 실패한다. 이를 자동화로 메우는 역동기화:

- **AMA 감지**: tsamx가 refresh를 수행하면 로컬 `.credentials.json`이 갱신된다. AMA store/reporter가
  credential fingerprint 변화(§tsamx `oauth.credential_fingerprint` — refresh token 해시 기반)를
  감지 → 갱신된 credential 세트를 매니페스트 KEK로 재암호화.
- **전송**: `AmaMessage`에 신규 `CredentialUpdate`(proto 변경, AmaMessage oneof 확장) — `{ams_account_id,
  EncryptedCredential(갱신본), server_credential}`. P2 보안 준수: AAD 바인딩(amsAccountId‖agentId),
  세션 서명·TLS. credential은 채널에만 실리고 로그·DB 평문 저장 금지(§7).
- **AMS 갱신**: 수신(`_apply_cred_update`) → observed_at 필수·미래 skew clamp → 테넌트 조회 → 소유권
  검증(이 서버에 active/inactive/quarantined 배정만; pending/recalling 배제) → key_id 일치 → sealed box
  개봉(AAD 로컬 재유도) → `crypto.encrypt_secret` **초크포인트** 경유 재암호화(F2 봉투암호화 자동 준수).
- **경합·단조성**: 설계 초안의 `credential_version`(정수) 대신 **`accounts.credential_observed_at`
  (에이전트 관측 시각, 벽시계 단조 래칫)** 채택 — 리부트 후에도 카운터 없이 생존, 원자적 조건부
  UPDATE(WHERE observed_at 가드)로 F3 다중 인스턴스·중복·롤백 재생을 거부. NULL = 최초 무조건 수락.
  (2026-08-09 소급 확인·승인 — as-built 우선, 정수 version 컬럼 없음.)
- **관찰 경계**: AMA resyncer는 활성 계정의 `.credentials.json`만 관찰한다. 활성이었다가 회전 직후
  tick 전에 전환·recall된 계정의 회전은 놓칠 수 있음 — 아래 폴백으로 정합성은 유지되므로 수용.
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
| recall | 대상이 활성이면 먼저 `tsamx switch <타계정>` → **기본(`purge_local_copy=false`, O2)**: `tsamx disable` + 매니페스트 레코드 `inactive`로 보존(빠른 재배정) / **`purge_local_copy=true`**: `tsamx remove` + 매니페스트 레코드 삭제 | 부재 시 성공 no-op |
| activate | `tsamx enable` + 매니페스트 상태 갱신 | 동일 상태면 no-op |
| deactivate | `tsamx disable` (크레덴셜 유지, 로테이션만 제외) | no-op |
| switch_now | `tsamx switch <num\|email>` (또는 `--strategy best`) | 이미 활성이면 no-op |
| set_mode | auto: scheduler 틱 시작 / manual: 틱 중지 | 값 동일 no-op |
| req_report | 즉시 §6.5 리포트 생성·전송 | 항상 수행 (조회는 멱등) |

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
        "sevenDay": { "pct": 44.0, "resetsAt": "2026-08-11T00:00:00Z" }
      },
      "usageFetchedAt": "2026-08-07T08:59:48Z"
    }
  ]
}
```

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
| O2 | recall 시맨틱 | **결정: disable만** (2026-08-08). 기본 `purge_local_copy=false` — `tsamx disable`+레코드 보존(빠른 재배정), `true`만 완전 삭제. §5.2·§6.3 반영 | ✅ 확정 |
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
