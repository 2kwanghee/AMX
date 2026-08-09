# ② 청구 하드닝 (F5 리뷰 이월)

> 상태: 대기 (① 완료 후 착수). 출처: F5 리뷰 B (BACKLOG G25~G28). 내부 청구를 실제로 신뢰하려면 ②가 선행돼야 한다.

## G25 — 테넌트 삭제 시 pending 청구 원장 소실 방지 — R2

**배경**: `billing_events.tenant_id` FK가 CASCADE라 테넌트 삭제 시 미export(pending) 원장이 조용히
소멸한다. `delete_tenant`의 가드 체인(`ams-server/app/services/inventory.py:110-140` — assignment·
account/server·admin·DEK 검사)에 billing 검사가 없다. DEK를 RESTRICT로 둔 논리(원장은 앵커)와 비대칭.

**할 일**: `delete_tenant` 가드에 pending billing_events 존재 검사 추가(있으면 삭제 거부, 기존 가드와
동일한 오류 관례) 또는 FK RESTRICT 전환 — 두 안 중 기존 가드 체인 방식이 기존 관례와 정합(T2).
exported 원장은 삭제 허용 여부를 함께 결정해 문서화.

**완료조건**: pending 이벤트가 있는 테넌트 삭제가 거부되는 테스트 + export 후 삭제 시나리오 테스트 통과.

## G26 — export 후 정정(void/재집계) 수단 — R2

**배경**: `ON CONFLICT DO NOTHING`이라 재스윕으로 기존 이벤트를 덮을 수 없고, exported 전이는
단방향이라 정정은 수동 SQL뿐이다. 내부 청구도 정정 플로우는 필요하다.

**할 일**: 최소 정정 수단 설계 — 권장 형태는 **원본 불변 + 반전/재발행 이벤트**(kind `usage_daily_void` 등,
회계 관례)이며 in-place 수정은 지양. global-admin 전용 REST. 과금 대상 사용자가 미정(A3/보류)이므로
**과도한 일반화 금지** — void + 해당 일 재집계까지만.

**완료조건**: void→재집계 흐름 테스트(원본 보존·순합 정확성·멱등) + 문서(§5.1 스키마 갱신) 통과.

## 부기 (이번 순위에 포함하지 않음, 착수 시 재평가)

- **G27** — 시계 앞점프 시 워터마크가 미래로 전진해 조용한 미청구 가능(되감김은 안전).
  `reported_at < watermark` 도착 행 카운터·경보 추가는 저비용이라 G25/G26 작업 시 함께 처리 고려.
- **G28** — 첫 실행/장기 다운 후 전 구간 `.all()` 일괄 로드(일 단위 chunk 권장) ·
  `_try_advisory_xact_lock` 중복 정의(DRY). 저심각, ④로 넘겨도 무방.
