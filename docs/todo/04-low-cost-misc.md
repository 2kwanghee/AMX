# ④ 나머지 (저비용·상시 처리 가능)

> 상태: 대기 (③ 완료 후, 또는 앞 순위 작업의 인접 R0 변경으로 함께 처리 가능한 것만 예외).

## 문서 정정 (stale 행 — 즉시 가능, R0)

- BACKLOG **C2 행**: "원시 KEK passthrough"는 stale — X25519 sealed box 구현 완료
  (`ama-agent/internal/crypto/crypto.go:199-214`, §7 반영). 완료 표기로 정정.
- BACKLOG **E3 행**: "Principal 훅 미구현"은 stale — P5 S1에서 구현 완료(`app/api/deps.py`). 완료 표기로 정정.

## 콘솔 완성도 (E)

- **E1** — 정책 편집 UI: 백엔드·BFF는 threshold/strategy/cooldown/hysteresis PATCH 지원, ams-web에
  편집 폼 부재. ServersPanel에 편집 어포던스 추가.
- **E2** — `GET …/servers/{sid}/events` BFF allowlist에 있으나 UI 소비자 없음. 이벤트 타임라인 뷰 추가.

## 운영 절차 (B2·B3 — 내재화 규모에서는 문서·절차만으로 충분)

- **B2** — tsamx 설치 인증: 내재화 1차는 읽기 전용 deploy key 공용으로 충분. 절차 문서화.
  (서버별 키/machine user/wheel 서빙은 서버 수 증가 시 — 보류 아님, 트리거 조건만 기록.)
- **B3** — tsamx 업스트림 동기화: `vendor/claude-swap-upstream` 3-way 비교 수동 병합 체크리스트 + 소유자 지정.

## G nits (BACKLOG G1~G24, G27~G28 중 미처리분)

전체 목록은 BACKLOG 참조. 손대는 파일이 겹치는 앞 순위 작업이 있으면 그때 함께 처리하는 것이 원칙
(단독 착수는 ④ 도달 후). 주요한 것만:

- G6 alert ack/open 좁은 레이스 · G8 AMA FindByEmail 중복 email 비결정 · G14 로그인 타이밍 오라클(SaaS 보류와 겹침)
- G22 F3 claim-before-write 지연 완화(별도 설계·검토 필요 명시됨) · G23 F3 문서 정정
- G27 billing 시계 앞점프 카운터·경보(②에서 처리 안 됐으면 여기서) · G28 billing chunk 로드·DRY
- **G24는 항목이 아니라 프로세스 규칙** — "각 병합 시 전체 e2e 게이트 필수". 이미 적용 중인 관행을 유지.
