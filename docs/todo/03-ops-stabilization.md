# ③ 운영 안정화 (회복 엣지 · 이벤트 무손실)

> 상태: 대기 (② 완료 후 착수). P3 reconcile로 부분 완화된 엣지들의 완전한 처리.

## D1 — recall 실패(DIVERGED/REJECTED) stranded 회복 — R2

**배경**: 실패한 recall이 어떤 재요청도 못 받고 영구 stranded — 설계상 recall 실패 회복이 미정의다.
reconcile-on-report가 상태 드리프트는 잡지만, 실패 상태의 배정을 재구동하는 경로가 없다.

**할 일**: recall 실패 상태의 회복 시맨틱 정의(§5.2 상태기계 확장) — 권장: 실패 배정에 대한
관리자 재시도 REST(`:recall` 재발행 허용) + reconcile이 실패 상태를 감지해 경보. 자동 재시도는
무한 루프 위험이 있어 횟수 제한 필수.

**완료조건**: DIVERGED/REJECTED로 만든 배정이 재시도 경로로 회수 완료되는 테스트 + 경보 발생 테스트.

## D2 — sent-미ack 명령 고착의 완전한 처리 — R2

**배경**: 에이전트가 수신 후 ack 전에 끊기면 명령이 "sent" 고착, 배정이 "delivering" 고착.
reconcile-on-report의 억제 해제 + F3 sent 스위퍼(`sweep_sent_timeouts`)로 부분 완화됐으나,
완전한 타임아웃 재시도 정책(재시도 횟수·백오프·최종 실패 전이)은 미정의.

**할 일**: 현행 스위퍼의 커버리지 갭을 먼저 측정(어떤 명령 유형이 몇 초 고착 가능한가) → 갭이 있으면
재시도 정책 추가, 없으면 "부분 완화 = 충분"으로 판정하고 문서화만. **측정 없이 재설계 금지.**

**완료조건**: 갭 측정 결과 문서 + (갭 존재 시) 재전송 후 중복 실행이 없음을 보이는 테스트(명령 멱등 전제 검증 포함).

## C1 — AccountEvent 전달 무손실화 — R2

**배경**: AMA outbox가 메모리 전용 + transport fire-and-forget이라 프로세스 재시작/단절 직후
인플라이트 이벤트 1건이 유실될 수 있다. 상태 정합은 usage report reconcile로 자가치유되므로
**손실되는 것은 감사 이벤트뿐** — F5 과금도 usage 원장 기반이라 과금 영향 없음(설계노트 위험 항목으로 이미 회피).

**할 일**: transport ack 기반 재설계(G12와 동일 작업 — AMS 앱레벨 ack + event_id dedup, proto 변경) 또는
AMA 디스크 outbox 영속화(proto 무변경, `reporter/outboxlog.go` 확장) 중 택1. **proto 무변경인 후자 우선
검토(T3)** — 전자는 A1의 proto 작업과 묶을 수 있으면 그때 병행.

**완료조건**: AMA 강제 재시작 시나리오에서 이벤트 유실 0 테스트 통과.

## 순서 제안

C1을 A1(①) 직후로 앞당길지는 ① 완료 시점에 재평가 — A1이 proto·transport를 여는 김에 ack 방식을
같이 넣는 것이 두 번 여는 것보다 쌀 수 있다. 기본은 D1 → D2 → C1.
