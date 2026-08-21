# AMX P2 "채널" 아키텍처 설계서

> 이 문서는 설계 시점 기록(as-designed)이며, 현행 동작의 기준은 `docs/AMX-DESIGN.md`다.

> REASONER 산출(2026-08-08). 구현 브리핑의 근거. SSOT는 `docs/AMX-DESIGN.md` —
> 상충 시 SSOT 우선. 확정 결정: O1 메모리 전용 KEK · O2 recall=disable(보존) · P2 전체 왕복.

## 핵심 설계 결정 8개

1. **gRPC는 별도 asyncio 프로세스, REST 큐를 DB로 소비.** FastAPI(동기 SQLAlchemy)와
   grpc.aio를 한 이벤트루프에 섞지 않는다. gRPC 서버는 `app/grpc/server.py`(별 포트 50051)
   독립 엔트리포인트. REST 전이 액션은 DB에 **명령 아웃박스 행**을 쓰고, gRPC 프로세스가
   그 행을 세션으로 밀어낸다. 두 프로세스의 유일한 결합점은 DB.
2. **명령 아웃박스 테이블 신설**(`agent_commands`) — proto 변경 아님, DB 스키마 추가.
   REST `:deliver`가 501을 벗고 INSERT → 배정 `pending→delivering`. gRPC가 CommandAck
   수신 시 `delivering→active`.
3. **SessionSetup은 applied_command_ids로 절대 억제 불가.** AMS는 모든 세션 시작 시
   Register 직후 무조건 SessionSetup(KEK) 하달. PR#1 재부팅 교착의 근본 해소.
4. **재조정은 "복호된 실상 보고"로만 구동.** 콜드스타트 시 KEK 미보유라 Register.accounts는
   빌 수 있음 → AMS는 빈 Register로 삭제 reconcile 금지, SessionSetup 이후 첫 UsageReport를
   실상 권위로 삼는다.
5. **O2 반영 — recall 기본은 disable+레코드 보존.** `detached`의 로컬 의미를 "레코드 삭제"에서
   "로테이션 제외+credential 레코드 보존"으로 재정의. `purge_local_copy=true`만 완전 삭제.
6. **AMA 매니페스트 = 단일 암호화 파일 + 평문 사이드카.** credential 레코드는 AES-256-GCM
   (KEK 메모리). `applied_command_ids`는 **평문 사이드카**(`applied.log`) — 비밀 아님, 재부팅 생존.
7. **AMA Go 데몬은 6개 컴포넌트 최소 골격.** transport/command/store가 R3.
8. **E2E는 실제 tsamx + 계정당 격리된 `CLAUDE_CONFIG_DIR`/`XDG_DATA_HOME`,** mock credential
   (가짜 OAuth JSON)로 10계정 A:3/B:5/C:2. usage API 호출은 stub.

## 1. gRPC 세션 프로토콜

```
AMA                                  AMS(gRPC proc)                DB
 │──dial TLS:50051─────────────────▶│
 │──Session() 열기──────────────────▶│
 │──AmaMessage{Register(auth=enroll_token or server_credential,│
 │   accounts, applied_command_ids, switch_mode)}─────────────▶│ servers.server_cred_hash
 │◀─AmsCommand{SessionSetup(server_credential?, keys=[KEK])}──│ (무조건, applied 무시)
 │──복호→매니페스트 오픈             │                            │
 │──AmaMessage{UsageReport(post-setup)}▶│─actual 확정, reconcile─▶│ assignments
 │◀─AmsCommand{Deliver/Recall/...}───│◀─agent_commands 폴링────────│
 │──CommandAck{Convergence}─────────▶│─assignments.state 전이─────▶│
 │  (heartbeat은 AMA→AMS 15s)        │
```
- 재연결·백오프: transport 지수백오프(1s→최대 30s, jitter). 두절 시 로컬 로스터로 스위칭
  계속, 로컬발 변경 거부, 이벤트 아웃박스 큐잉.
- 단절 감지: heartbeat 15s + gRPC keepalive. AMS는 `last_seen_at` 갱신, 3틱 누락 시 offline.

## 2. enroll 핸드셰이크

- **경로 A(최초)**: Register.auth=`enroll_token`(1회성). AMS `hash_token()` 비교 +
  `enroll_token_expires_at` 확인 → 통과 시 `crypto.new_token()`로 장수명 `server_credential`
  생성, `server_cred_hash` 저장, `enroll_token_hash=NULL`(소진) → **SessionSetup.server_credential**로
  반환. `server_id`/`agent_id`를 servers 행에 기록.
- **경로 B(재접속)**: Register.auth=`server_credential` → `server_cred_hash` 비교.
- **tenant 바인딩**: server 행의 `tenant_id`가 곧 세션 tenant. 클라이언트 제공 tenant 불신.
  이후 하달 명령 모두 `agent_commands.tenant_id == 세션 tenant` 필터.

## 3. 명령 멱등·수렴

- **처리로그 위치**: `applied.log`(평문 사이드카, JSON lines, command_id + convergence + ts,
  최근 128개 링). 매니페스트(암호화)와 분리 — 재부팅 후 KEK 없이도 읽혀야 함.
- **재전송 no-op**: `command_id ∈ applied.log` **AND** 실상==desired → 효과 no-op, CONVERGED
  재회신. 하나라도 불충족(재부팅으로 실상 소실) → 재실행.
- **Convergence**: 실상==요청 → CONVERGED / 크리티컬섹션 진행중 → PENDING / 부분·불일치 →
  DIVERGED / 서명·stale·타tenant → REJECTED.
- **applied_command_ids × 메모리 KEK 3규칙(PR#1 해소)**:
  1. SessionSetup은 command_id를 갖되 applied 게이트 **영구 제외** — 매 세션 무조건 하달.
  2. 콜드스타트 Register.accounts 빌 수 있음 → AMS는 **빈 Register로 삭제 reconcile 금지**,
     SessionSetup 후 첫 UsageReport를 실상 권위로.
  3. deliver 재하달 억제는 `command_id ∈ applied_command_ids` **AND** 보고된 actual에 그 계정
     존재 — 둘 다일 때만. 재부팅 후 actual 비면 억제 해제되어 재하달.

## 4. AMA 암호화 스토어

- **매니페스트**(단일 파일 `manifest.enc`): 레코드 배열, 각 = `{amsAccountId, email,
  allocationStatus, encryptedCredential{alg,ciphertext,nonce,keyId}, amsSignature(Ed25519),
  receivedAt}`. AAD = **로컬 유도** `(amsAccountId ‖ agentId)` — proto의 `aad_*`는 비교 전용, 입력 금지.
- **KEK 수명주기**: SessionSetup에서 수신 → 메모리만. 재부팅→소실→재연결 SessionSetup
  재수신까지 복호 불가. `revoked_key_ids`로 회전, `keys` 다중으로 오버랩 회전.
- **tsamx 파생사본 정합**: 매 리포트 틱 `tsamx list --json` ↔ 매니페스트 대조. tsamx-only →
  `remove`. manifest-only → 보관 credential을 `~/.claude/.credentials.json`+`~/.claude.json`
  기록 후 `tsamx add` 재주입.
- **서명 검증**: 명령·매니페스트 레코드 모두 빌드 내장 Ed25519 공개키로 검증 후에만 적용.

## 5. AMS gRPC 서버 통합

- **공존**: 같은 `ams-server` 패키지, 별 프로세스/별 포트(REST 8000, gRPC 50051). 결합은 DB로만.
- **세션 레지스트리**: gRPC 프로세스 인메모리 `dict[agent_id → stream_handle]`(단일 인스턴스 전제).
- **REST 전이 → 명령 큐**: `:deliver` → `agent_commands`(status=queued) INSERT +
  `assignments pending→delivering` + `pending_command_id` 세팅(컬럼 존재). gRPC 프로세스가
  `agent_commands WHERE status=queued AND server 온라인` 폴링(0.5s) 또는 LISTEN/NOTIFY →
  세션 push → CommandAck 수신 시 `agent_commands.status=acked` + `assignments` 전이.
- **state↔명령**: deliver→delivering→(ack CONVERGED)→active|inactive(desired_status),
  recall→recalling→(ack)→detached, activate/deactivate→SetAccountActive→active/inactive.

## 6. AMA Go 데몬 골격

모듈 `github.com/2kwanghee/AMX/ama-agent`, `contracts/gen/go`(amxv1) import.
- `internal/transport`: `Dial()`, 재연결 루프, `Send(AmaMessage)`, `Recv() AmsCommand` 채널.
- `internal/command`: 디스패처 `Handle(AmsCommand) CommandAck` — 서명검증→멱등(applied.log)→
  store/bridge→convergence.
- `internal/store`: `manifest.enc` R/W, AES-GCM, KEK 홀더(메모리), `applied.log`.
- `internal/tsamx`: `Add/Remove/Enable/Disable/Switch/List/Status` = `exec.Command`+`--json`.
  계정별 `CLAUDE_CONFIG_DIR`/`XDG_DATA_HOME` env.
- `internal/reporter`: 5분 폴링(`list --json` 재직렬화) + 즉시 이벤트 + 아웃박스.
- `internal/crypto`: Ed25519 검증, AEAD, KEK unwrap.
- `cmd/ama/main.go`: 조립 + scheduler(P3에서 채움, P2는 틱 미구동).

P2 필수 = deliver/recall/activate/deactivate + Register/SessionSetup/UsageReport/CommandAck.
scheduler·switch_now·set_mode는 골격만.

## 7. 구현 순서·트랙

- **T-A (AMS gRPC 서버, R3)**: `app/grpc/server.py` + `app/services/reconcile.py` +
  `agent_commands` 모델·alembic. enroll 인증·tenant 필터·서명.
- **T-B (AMA Go)**: transport→store→command→bridge→reporter. store/command/crypto=R3,
  transport/reporter=R2.
- **T-C (REST 배선, R2)**: `assignments.py` 501 제거 → `agent_commands` INSERT + 전이.
- 마일스톤: M1 세션+enroll+SessionSetup 왕복 → M2 단일 deliver CONVERGED → M3
  recall/activate/deactivate → M4 10계정 E2E.

## 8. 테스트 전략

- **E2E**: docker-compose(postgres+ams-rest+ams-grpc) + AMA Go 바이너리 3개 각각 격리된
  HOME/`CLAUDE_CONFIG_DIR`/`XDG_DATA_HOME`. **실제 tsamx 설치**, credential은 mock OAuth JSON,
  usage API는 tsamx stub 캐시로 우회(`cache/usage.json` 프리시드) — 실 Anthropic 호출 금지.
- **완료판정**: 10계정 생성→A:3/B:5/C:2 배정→일괄 deliver→3 AMA의 `tsamx list --json`이 정확히
  3/5/2 보유 + 배정 all `active`→recall→`detached`+로컬 disable 확인.
- **회귀 불변식**: 교차tenant 명령 REJECTED, AAD 오바인딩 복호 실패, 서명위조 REJECTED,
  재부팅(KEK소실)후 SessionSetup 재수신→복호 재개.

## 9. 미해결·위험

- **O9 refresh 회전 = 폴백 확정(2026-08-08)**: 재배정 시 §5.5 재인증 허용을 기본으로 구현.
  실계정 판별은 사용자 몫으로 이월. E2E는 mock이라 무관.
- **O5 러너 무중단(P2 배포)**: deliver 크리티컬 섹션 오과금 — 배포 설계에서 러너 일시정지/파일락.
- **KEK 부트스트랩 순환**: SessionSetup 전 빈 Register 규칙을 AMS reconcile이 반드시 지켜야 함.
  리뷰 필수 포인트.
- **DB 폴링 vs LISTEN/NOTIFY**: P2는 폴링 허용(지연 0.5s). 부하 시 재작업.

## 현행 대조 (2026-08-22)

설계 당시 확정과 현재 코드의 일치 여부다. 이 문서는 as-designed 기록이라 본문은 그대로 두고, 달라진 곳만 아래에 모은다.

| 항목 | 설계(P2) | 현행 | 근거 |
|---|---|---|---|
| 결정2 명령 아웃박스 | `agent_commands` 신설, REST가 INSERT하고 gRPC가 소비 | 그대로 | `ams-server/app/models.py:470,483` |
| 결정3 SessionSetup 무조건 하달 | applied 억제 불가, Register 직후 항상 | 그대로 | `ams-server/app/grpc/server.py:9` |
| 결정5 recall 기본 = disable+레코드 보존 | O2 초안: 로테이션 제외만, `purge_local_copy=true`만 완전 삭제 | **변경됨.** 회수는 provider 무관 항상 purge. `recall_purges_local_copy`가 상시 True를 돌려 detached↔inactive 불일치와 비용 오배분을 없앴다(2026-08-14 사용자 지시) | `ams-server/app/services/commands.py:134-157,216` |
| 결정6 암호화 스토어 + 평문 사이드카 | AES-256-GCM(KEK 메모리) 매니페스트 + `applied.log` | 그대로 | `ama-agent/internal/store/`, `internal/reporter/` |
| O1 메모리 전용 세션 KEK | 세션마다 KEK를 에이전트 메모리에만 | 그대로. 추가로 C2 per-agent NaCl sealed box 봉인이 위에 올라감 | `ams-server/app/grpc/server.py:10,323-340` |
| (신규 축) 계정 풀 상태 | P2엔 없음 | `accounts.pool_state` 6종과 풀 테이블(마이그레이션 0028~0031)이 배정 상태기계와 별개 축으로 추가됨. 배정(pending~detached)은 불변, 풀은 그 위의 순환 관측 | `ams-server/app/models.py:97`, AMX-DESIGN §5.8 |
