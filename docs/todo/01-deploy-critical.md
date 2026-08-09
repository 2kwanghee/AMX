# ① 배포 필수 (내재화 실배포 직전 필수)

> 상태: 진행 중. 트랙 구성: B1 ∥ B4 (독립, 병렬 가능) → A1 (최대 작업, R3).
> C2는 조사 결과 **이미 구현 완료**라 이 순위에서 제외했다(아래 "C2 확인 기록" 참조).

## B1 — 러너 진입점 강제 (O5 잔여) — R1

**배경**: deliver 크리티컬 섹션(§6.3) 동안 `~/.claude/.credentials.json`이 순간 교체된다.
주 방어(이전활성 복귀 + 원자적 쓰기)와 보조 방어(`deploy/amx-claude` flock 래퍼)는 구현·병합 완료.
**잔여 = 래퍼를 안 거친 직접 `claude` 실행의 sub-second 오과금 창**: 코드로는 못 막고 배포에서 강제해야 한다.

**할 일**
- 배포 시 러너 진입점을 래퍼로 강제하는 설치 메커니즘(PATH 셰도잉/alias/심링크 중 택1, 근거 명시)
- 러너와 AMA가 같은 `~/.claude`를 보도록 배포 검증(다른 HOME/컨테이너 분리 감지)
- 강제 상태를 점검하는 검증 스크립트(설치 후 + 주기 점검 겸용)
- `docs/DEPLOYMENT-RUNNER.md` 갱신

**완료조건**: 검증 스크립트가 (a) 진입점이 래퍼임 (b) 러너·AMA의 `~/.claude` 동일함을 판정하고,
비강제 상태를 심은 테스트에서 실패를 정확히 검출한다.

## B4 — TLS 실배포 구성 (D9) — R1

**배경**: 코드 경로는 완성 — cert/key 제공 시 `add_secure_port`, 미제공 시 `AMX_GRPC_ALLOW_INSECURE=1`
opt-in fail-closed, AMA 쪽 TLS/tls 테스트(`transport_tls_test.go`) 존재. **잔여 = 실배포 cert 발급·배포 절차.**

**할 일**
- 내재화용 사설 CA + 서버 cert 발급 스크립트(`deploy/` 하위, openssl 기반, 갱신 절차 포함)
- AMA 측 CA 신뢰 배포 절차, (옵션) mTLS 구성 예시 — §7 위협 경계(능동 MITM의 agent_public_key 치환은 TLS/mTLS가 방어)가 근거
- `docs/DEPLOYMENT-TLS.md` 갱신(발급→배포→검증→갱신 런북)

**완료조건**: 스크립트로 발급한 cert로 AMS 기동 + AMA 접속이 TLS로 성립하고(E2E 또는 스모크 스크립트),
`AMX_GRPC_ALLOW_INSECURE` 미설정 상태에서 평문 접속이 거부된다.

## A1 — O9 credential 역동기화 — **소급 확인 결과 구현 완료** (2026-08-09)

착수 전 설계 조사(REASONER)에서 **A1이 이미 구현·병합 완료**임을 확인했다 — BACKLOG A1 행이
stale였다(C2·E3와 동일 사례). as-built: proto `CredentialUpdate`(AmaMessage 15, 3-lang codegen 반영),
AMA `internal/resync/`(fingerprint 감지, lock 밖 전송, 수락 시 baseline 전진), AMS
`_apply_cred_update`(소유권·key_id·sealed box AAD 재유도·`crypto.encrypt_secret` 초크포인트·원자적
조건부 UPDATE), 마이그레이션 0005, E2E `e2e/test_o9_resync_e2e.py`. 상세는 §5.7(정정 완료).

**설계 이탈 수용 기록**: 초안의 `credential_version`(정수) 대신 `credential_observed_at`(벽시계 단조
래칫) — 리부트 생존·시계 되감김 안전측. as-built 우선으로 승인, §5.7에 기록.

**A1 잔여 작업** (이것만 남음)
1. **가용성 격리 패치 (R2, 진행 중)**: `_apply_cred_update`의 `crypto.encrypt_secret`가 try/except 밖 —
   DEK 부재/KEK 오류 시 세션 스트림 전체가 드롭. 해당 건만 거부하고 스트림 유지하도록 격리.
   완료조건: DEK 미구성 상태 cred_update 수신 시 스트림 유지 테스트 통과.
2. **ADVERSARY 소급 반증 — 완료 (2026-08-09)**: 6개 각도 중 핵심 방어선(위조 주입·롤백 재생·
   AMS 경합·암호 경계·NULL 선점) 전부 반증 불성립 — 병합본 견고 판정. 성립 발견은 BACKLOG
   **G29**(자원 소모, 중 — 내재화에선 위협 낮음, 외부 노출 전 필수), **G30**(baseline 무ack 전진, 중 —
   C1/G12 ack 인프라와 공유 가능), **G31**(저심각 2건)로 이월.
3. 문서 정정(§5.7·§8 O9·BACKLOG A1) — 완료.

## C2 확인 기록 (2026-08-09, 이 순위에서 제외)

BACKLOG C2 행("WrappedKey 원시 KEK passthrough")은 **stale**. 실코드는 세션 KEK를 에이전트별
ephemeral X25519 **NaCl sealed box**로 봉인한다 — `ama-agent/internal/crypto/crypto.go:199-214`(UnwrapKEK가
raw KEK·타 키 봉인·변조를 전부 거부), AMS 측 봉인 경로, AMX-DESIGN §7 In-transit 행 반영 완료(c2-kek-wrap 병합).
남은 것 없음. BACKLOG 행 정정은 ④(04-low-cost-misc.md)에서 일괄 처리.
