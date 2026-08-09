# AMX 실행 계획 (todo)

> 2026-08-09 사용자 확정. P0~P5 전 Phase 병합 완료(main b95b0bc) 직후의 잔여 작업 실행 계획.
> **이 폴더가 실행 순서·수용 기준의 SSOT**다. `docs/BACKLOG.md`는 이월 항목의 원장(출처·이력),
> 여기는 "무엇을 어떤 순서로, 언제 끝났다고 판정하는가"를 담는다. 항목 번호(A1, B4, G25 …)는 BACKLOG와 공유한다.

## 방침 (2026-08-09 확정)

1. **내재화 우선, 상용 SaaS 보류.** 사내 운영(내재화)에 필요한 작업만 진행하고,
   상용 SaaS 준비 성격의 항목은 [on-hold-saas.md](on-hold-saas.md)로 보류한다.
   재개 시점 = 내재화 안정화 기간 종료 후 사용자 결정.
2. **우선순위 ① > ② > ③ > ④ 순서대로 진행한다.** 앞 순위가 끝나기 전에 뒤 순위에 착수하지 않는다
   (단, 같은 순위 안의 독립 트랙은 병렬 가능).

## 진행 현황

| 순위 | 내용 | 파일 | 상태 |
|---|---|---|---|
| ① | 배포 필수 — B1(PR #23) · B4(PR #24) · A1(기구현 확인 + 가용성 패치 PR #25 · 소급 반증 완료) | [01-deploy-critical.md](01-deploy-critical.md) | **작업 완료** — PR 3건 머지 대기 |
| ② | 청구 하드닝 — G25(테넌트 삭제 가드) · G26(export 정정 수단) | [02-billing-hardening.md](02-billing-hardening.md) | 대기 |
| ③ | 운영 안정화 — D1(recall 실패 회복) · D2(sent-미ack 고착) · C1(이벤트 무손실) | [03-ops-stabilization.md](03-ops-stabilization.md) | 대기 |
| ④ | 나머지 — E1·E2(콘솔), B2·B3(운영 절차), 문서 정정, G nits | [04-low-cost-misc.md](04-low-cost-misc.md) | 대기 |
| 보류 | 상용 SaaS 준비 — F2 실 KMS, SaaS 하드닝, A2, A3 | [on-hold-saas.md](on-hold-saas.md) | 보류 |

## 완료된 것 (요약)

- **로드맵 §9 전 Phase**: P0 계약 · P1 인벤토리+OAuth 등록 · P2 채널 E2E · P3 스위칭·reconcile ·
  P4 콘솔·경보 · P5 SaaS 준비(E3 Principal, F1 RBAC, F2 봉투암호화(local KEK), F3 멀티인스턴스,
  F4 정책 중앙화, F5 과금 outbox) — 전부 병합·완료판정 통과.
- 회복 트랙(recovery), C2 세션 KEK sealed box 래핑, C3 BFF allowlist 하드닝도 완료.
  (BACKLOG의 C2·E3 행은 stale였음 — 정정은 ④에 포함, [04-low-cost-misc.md](04-low-cost-misc.md) 참조.)
- 완료율: 로드맵 기준 100% · 설계 전체 기능 기준 약 85% · 상용 준비도 기준 약 70%(보류 방침으로 당분간 비목표).

## 규칙

- 각 항목은 완료 시 이 표와 해당 파일의 상태를 갱신하고, BACKLOG의 원장 행에도 완료 표기를 남긴다.
- 리뷰 등급은 전역 지침의 리스크 등급표(R0~R3)를 따른다. 각 항목 파일에 등급을 명시했다.
