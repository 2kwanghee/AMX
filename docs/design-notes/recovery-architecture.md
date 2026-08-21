# 운영 안정화 회복 설계서 (D1·D2·C1)

> 이 문서는 설계 시점 기록(as-designed)이며, 현행 동작의 기준은 `docs/AMX-DESIGN.md`다.

> REASONER 산출(2026-08-09). 구현 브리핑 근거. SSOT는 `docs/AMX-DESIGN.md` — 상충 시 SSOT 우선.
> **proto 변경 불필요.** C1 완전무손실(b2, AMS 앱-ack)만 proto를 건드리므로 이번 범위 제외·이월.

## 핵심 결정 6개
1. **proto 변경 불필요** (D1·D2·C1). 완전무손실(b2)만 proto — 이월.
2. **D1**: recall 실패는 `recalling` 유지(`pending_command_id=NULL`+`last_error`=정착·비인플라이트 표식) → ① `reconcile_from_report`가 정착 recalling을 포함해 자동 재recall(`CORRECTION_RECALL`+`CORRECTION_CAP` 재사용), ② `request_recall` REST가 정착 recalling 수락(수동 탈출구). 신규 상태 없음.
3. **D2**: `agent_commands.status="sent"` 타임아웃 스윕(기존 offline sweeper 루프에 합류) → `sent→queued` 재큐(멱등, 같은 command_id). `send_attempts` 상한 초과 시 `failed`+배정 정착 되돌림.
4. **C1**: Outbox를 디스크 영속 로그(평문 사이드카, 이벤트는 credential-free)로 승격 + 전송 확인(stream.Send 성공) 후에만 삭제. 두 유실창(재시작·sendCh드롭) 닫음. AMS 앱-ack는 이월.
5. **공용화**: D1·D2는 AMS "유계 멱등 재시도"(카운터+스윕+CAP) 원칙 공유. C1은 AMA 독립. 크로스언어 추상화 금지.
6. **트랙**: AMS(D1+D2) ∥ AMA(C1) 병렬. AMS 내부는 파일 중첩이라 단일 구현자 순차(D2→D1).

## 1. D1 — recall 실패 stranded (R3)
**고착**: `apply_ack` DIVERGED/REJECTED가 deliver만 `pending` 되돌림(reconcile.py:122-125), recall은 `recalling` 잔존. REST 액션 precondition에 `recalling` 부재, `reconcile_from_report` 쿼리도 `recalling` 제외(reconcile.py:240) → 영구 stranded.

**설계**:
- 쿼리 확대: 대상 = `(active,inactive,quarantined,detached)` **OR `(recalling AND pending_command_id IS NULL)`**. 인플라이트 recall(pending_command_id 존재)은 제외 → 살아있는 명령과 무경합.
- 정착 recalling desired = `ABSENT`:
  - `actual != ABSENT`(로컬 잔존) → `CORRECTION_RECALL` 재큐(in-flight 스킵 + CAP).
  - `actual == ABSENT`(이미 제거, ack만 DIVERGED) → detached 정착 + account→available(명령 없이 상태만).
- REST 탈출구: `request_recall`이 `state=='recalling' AND pending_command_id IS NULL` 수락.
- CAP 소진 후 명령 미큐 + drift 경보만(피드백 루프 불가).

**완료판정**: 최초 recall이 DIVERGED로 실패한 배정이 ≤N 리포트주기 내 `detached`(account `available`)로 수렴 + 정착 recalling에 `POST …:recall`이 200.

## 2. D2 — sent-미ack 고착 (R2)
**고착**: 폴 루프가 `mark_sent`로 queued→sent(server.py:291), `fetch_queued`는 `status=='queued'`만(commands.py:308-311) → `sent`는 재전송 대상 아님. 에이전트 수신-미적용 단절 시(applied_command_ids 미포함) suppress_applied도 못 걷어 `sent` 영구 잔존, 배정 delivering/recalling 고착.

**설계** (기존 `_offline_sweeper` server.py:1014에 sibling 합류 — 새 타이머 없음):
```
sweep tick:
  for cmd in agent_commands where status='sent' and sent_at < now - SENT_ACK_TIMEOUT:
    if cmd.send_attempts < MAX_SEND_ATTEMPTS:
        cmd.status='queued'; cmd.sent_at=NULL; cmd.send_attempts += 1   # 멱등 재전송
    else:
        cmd.status='failed'
        deliver→배정 'pending' / recall→D1경로 / act·deact→pending_command_id=NULL
        server-scoped(set_mode·set_policy·req_report)→마킹만(다음 세션 재천명 자가치유)
```
- 멱등: 같은 command_id 재전송 → 에이전트 applied.log dedupe → CONVERGED 재-ack(§6.3).
- **reconcile 무경합**: reconcile는 정지상태(active/inactive/quarantined/detached)만, D2 스윕은 인플라이트(delivering/recalling)만 — 배정 상태로 분할.
- suppress_applied 호환: suppress 쿼리가 이미 `sent` 포함(reconcile.py:184).
- 마이그레이션: `agent_commands.send_attempts INT DEFAULT 0`(additive, alembic 1건).

**완료판정**: ack 없이 `sent` 잔존 명령이 SENT_ACK_TIMEOUT 후 재큐→재전송→CONVERGED로 배정 정상 전이 + MAX_SEND_ATTEMPTS 초과 시 `failed`+배정 재발행 가능.
- 타임아웃값: 2–3×heartbeat(60–90s) 권장.

## 3. C1 — AccountEvent 무손실 (R3)
**두 유실창**: (W1) Outbox 인메모리(reporter.go:183) → 재시작 시 소실. (W2) Flush가 TrySend(sendCh 적재)를 성공 간주해 큐 제거(reporter.go:240-247)하나, sendCh에서 꺼낸 뒤 stream.Send 전/중 단절 시 1건 소실(transport.go:245-261).

**권장 = (a)디스크 영속 + (b1)stream.Send 확인 후 삭제, (b2)AMS 앱-ack 이월**:
- Outbox를 append-only 디스크 로그(stateDir, applied.log 사이드카 패턴 재사용; 이벤트는 비밀 아님). Enqueue=디스크 append, 시작 시 로드, **확인된 전송 후에만 삭제**.
- 삭제 게이트 = stream.Send 성공. gRPC 스트림은 단일 송신 고루틴 필수 → sendItem에 `done chan error` 부착, 상류 고루틴이 stream.Send 결과를 done으로 회신. 드레인은 done 확인 후 디스크 삭제. usage는 기존 TrySend(드롭 안전) 유지.
- **최대 난제(R3)**: 세션 teardown 시 미결 done이 **반드시 에러로 resolve**(session 종료 경로에서 sendCh 잔여를 에러 배수) → 드레인 데드락 방지. 로그 크래시정합(라인단위 atomic append + 손상tail 스킵).
- **잔존창**: stream.Send 성공(소켓 flush)했으나 AMS 커밋 전 crash — 결정8 근거로 수용(상태는 5분 리포트+reconcile 자가치유, all_exhausted/drift는 sync_from_report 재도출 server.py:693). audit(switch_event 행)만 극히 드물게 손실.
- **AMS 이벤트 dedup 없음** 주의: (b1)의 드문 재전송이 switch_event 스냅샷 중복삽입 가능(audit 중복, 상태 무영향) — 수용.

**완료판정**: 재시작 직전 큐잉된 AccountEvent가 재시작 후 정확히 1회 전달(AMS usage_snapshots에 존재) + flush 중 스트림 드롭이 이벤트를 잃지 않음.

## 4. 구현 순서·트랙·리스크
- **트랙 AMS(D1+D2, 단일 구현자 순차)**: D2 스윕+send_attempts(alembic) → D1 reconcile recall-retry+REST. **D1=R3**, **D2=R2**.
- **트랙 AMA(C1, 병렬)**: 디스크 Outbox+확인채널 transport. **R3**.
- **테스트**(P2/P3 docker-compose E2E 확장, 신규 AMX_TEST_* 훅): D1=recall DIVERGED 강제→재recall 수렴; D2=ack 억제+단절→타임아웃 재큐 수렴, cap 초과→failed; C1=큐잉 후 kill-9 재시작→디스크 재적재 전달, flush중 Send실패→보존·재전달.
- **미해결/위험**: (i) D1 리포트구동 detached 정착은 `pending_command_id IS NULL` 게이트+CAP 필수. (ii) D2 타임아웃값(60–90s). (iii) **C1 done-resolve 누락 시 드레인 데드락 — 최우선 검증**. (iv) 완전무손실(b2)·quarantine 리포트화는 분리 이월(proto/추가범위).

## 이월 (완료판정 비차단)
- C1 완전무손실(b2 AMS 앱-ack + event_id dedup, proto 변경) — BACKLOG.
- quarantine 경보 리포트화(alerts.sync_from_report에 quarantined 추가) — 소규모 인접 하드닝, BACKLOG.

## 현행 대조 (2026-08-22)

| 항목 | 설계 | 현행 | 근거 |
|---|---|---|---|
| D1 recall 실패 stranded | `recalling` 유지(`pending_command_id=NULL`+`last_error`) + 자동 재recall(CORRECTION_CAP) + REST 수동 탈출구 | 구현됨. `recall_retry_count` 컬럼, reconcile 재recall과 성공 시 리셋, force recall 탈출구 | `ams-server/app/models.py:459`, `app/services/reconcile.py:192,249` |
| D2 sent-미ack 고착 | 타임아웃 스윕이 `sent→queued` 재큐(멱등), `send_attempts` 상한 초과 시 failed | 구현됨 | `ams-server/app/models.py:514`, `app/services/reconcile.py` |
| C1 AccountEvent 무손실 | Outbox를 디스크 영속 로그로 승격, 전송 확인 후 삭제 | 구현됨. 잔존창은 stream.Send 성공 후 AMS 커밋 전 crash뿐(결정8 수용), 앱-ack(b2)는 이월(BACKLOG G12) | `ama-agent/internal/reporter/outboxlog.go`, `internal/reporter/outbox_disk_test.go` |
| CORRECTION_CAP | reconcile 자동 재하달 유계(기본 3) | 그대로. `AMX_RECONCILE_CORRECTION_CAP` 기본 3 | `ams-server/app/services/reconcile.py:64` |
