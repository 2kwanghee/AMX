# AMX P5 SaaS 아키텍처 설계

> 이 문서는 설계 시점 기록(as-designed)이며, 현행 동작의 기준은 `docs/AMX-DESIGN.md`다.

> REASONER 산출(2026-08-09). SSOT는 `docs/AMX-DESIGN.md`. P5는 여러 독립 항목의 묶음 — 단계 분할.

## 결론 — 우선순위·첫 단계
- **첫 단계 = S1 E3 Principal 리팩터** (F1 선행, 인프라 결정 0, 무행위변경 순수 리팩터, R2).
- **MVP 순서**: E3 → F1(RBAC) → F2(봉투암호화) ∥ F3(멀티인스턴스) → F4 → F5. F1이 SaaS 정의(테넌트별 격리 관리자)라 최우선. F2·F3 독립 병렬. F4·F5 비필수 후순위.
- **F3 재평가**: 명령 전달은 이미 DB-디커플(REST는 `agent_commands` enqueue만, `_online`은 관측용, online/offline은 `servers.last_seen_at`서 파생). AMS는 라우팅 관점 stateless → F3 = `FOR UPDATE SKIP LOCKED` + 스위퍼 단일화로 축소(Redis+라우팅 아님).

## 단계 분할
| 단계 | 내용 | R |
|---|---|---|
| S1 | **E3 Principal 리팩터** (첫 착수, 무행위변경) | R2 |
| S2 | F1 admins/roles + Principal 스코핑 집행 | R3 |
| S3 | F2 KMS 봉투(DEK 간접화) — S2 후, O9 쓰기경로 조율 | R3 |
| S4 | F3 SKIP LOCKED + 스위퍼 단일화 | R2 |
| S5 | F4 proto(SetPolicy cooldown/hysteresis) | R2 |
| S6 | F5 billing_events outbox(usage_snapshots 원장 기반) | R2 |

## 의존 그래프
```
E3(Principal) ──► F1(RBAC) ──┬──► F2(봉투암호화)  [+ A1/O9 쓰기경로 조율]
                              └──► F3(멀티인스턴스, 독립)
proto: F4(SetPolicy 필드) ── 독립
데이터: F5(billing) ── usage_snapshots 위, 후순위
```
E3만이 F1을 막음. 나머지 독립. F2는 O9(A1)와 `encrypt_secret` 쓰기 경로 공유 — 먼저 착수하는 쪽이 추상화 확정.

## 결정 포인트 (사용자 확인용)
1. **F1 인증 방식**: 자체 admins 테이블+bcrypt(권장 MVP) / OIDC-SSO(Auth0·Cognito) / API키
2. **F1 RBAC 입도**: 2-role(global-admin·tenant-admin, 권장) / 세분 권한
3. **F2 KMS**: AWS KMS(AWS 배포 시 권장) / Vault Transit(멀티클라우드·온프렘 권장) / 자체(지양 — §7 키분리 약화)
4. **F2 DEK 로테이션**: lazy 재암호(다음 deliver/O9-push 시, 권장) / 전체 rewrap 배치
5. **F3 presence 백엔드**: 도입 안 함(권장 MVP, 현 구조 이미 안전) / Redis pub/sub(직접-push 최적화 시)
6. **F5 과금 대상·스키마**: **확정 — 내부 청구.** 데이터 원천 = `usage_snapshots` 원장(reconcile 반영, C1 유실 회피). `billing_events` outbox를 테넌트×닫힌 UTC 일로 집계(멱등 앵커 `UNIQUE(tenant_id,kind,period_start)`), REST list/export만 노출. 외부 결제(Stripe 등) 미연동·proto 무변경.

## proto/SSOT 영향
- **proto = F4만**: SetPolicy에 `cooldown_seconds`·`hysteresis_pct` 추가. (F1·F2·F3·F5 proto 무변경 — RBAC은 REST 평면, 봉투암호화는 at-rest, wire는 세션 KEK sealed box로 독립.)
- **§7**: F1이 REST 인증 행·openapi securitySchemes(단일→다중관리자 RBAC), F2가 At-rest SaaS 행(테넌트 DEK/KMS).
- **§5.1**: F1이 `admins`/`principals`+테넌트 매핑 테이블, F2가 테넌트별 wrapped-DEK 컬럼/테이블.
- **§5.4**: F3이 "다중 AMS" 문단을 실상(DB-큐 stateless + SKIP LOCKED)으로.

## 위험
- **F1 격리 우회**: Principal 스코핑 한 곳 누락 시 교차 테넌트 노출 → Principal이 허용 tenant 집합을 공통 의존성에서 강제, §5.1 복합 FK backstop 유지.
- **F2 가용성**: KMS 장애 시 전 계정 복호 실패 → unwrapped-DEK 메모리 캐시 + 재시도. **O9 쓰기 경로가 DEK 우회하면 이중 암호 붕괴 — F2 착수 전 O9 쓰기경로 확정 필수.**
- **F3 split-brain**: 재연결 순간 stale+신규 세션 중복 `fetch_queued` → `FOR UPDATE SKIP LOCKED` + 멱등 command_id. 스위퍼 advisory-lock 단일화.
- **F5 과금 정확성**: best-effort AccountEvent는 C1 유실로 과소청구 → reconcile된 usage_snapshots 원장 기반.

## S1 E3 상세 (첫 구현)
- **목표**: `auth.py:require_admin(-> None)` → `-> Principal` 반환형, 동작 무변경. Principal은 현 단일 관리자 = `Principal(kind="admin", tenant_ids="*")`.
- **소유**: `ams-server/app/core/auth.py`(Principal dataclass + 반환), `app/api/v1/*.py`(의존성 시그니처 `principal: Principal = Depends(require_admin)`, 사용은 S2+). BFF 무변경.
- **완료조건**: 기존 인증 테스트 전부 통과(401/200 불변) + 신규 테스트(유효→Principal 반환, 무효→401). **행위 변경 0.**
- **금지**: RBAC 스코핑·admins 테이블·proto·크립토 일절(S2+). 복합 FK·gRPC 무변경.

## 미해결
- F4 tsamx cooldown/hysteresis config 키 실재 여부 미확인(F4 착수 전 tsamx 소스 확인).
- F5 과금 대상 사용자·요금 정책 미정(스키마·집계는 구현 완료; 외부 결제 연동은 도입 시점 재검토).
