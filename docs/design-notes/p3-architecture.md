# AMX P3 "스위칭 제어" 아키텍처 설계서

> REASONER 산출(2026-08-08) + O4-C 델타 통합. 구현 브리핑의 근거. SSOT는 `docs/AMX-DESIGN.md` —
> 상충 시 SSOT 우선. 확정: O4-C 하이브리드(threshold+strategy 하달, cooldown/hysteresis 로컬).

## 핵심 설계 결정 9개

1. **P3는 확정 proto 안에서 구현 가능** — SetSwitchMode/SwitchNow(strategy)/RequestReport/AccountEvent(all_exhausted)
   이미 존재. **유일한 proto 추가 = O4-C의 `SetPolicy`(cmd 17)** (아래 O4-C 섹션).
2. **usage 인제스트가 모든 것의 선행.** reporter 5분 틱을 실제 구동, AMS `_store_usage`를 usage_snapshots
   저장에서 그치지 말고 **reconcile 입력**으로 배선. P2에서 미발화된 rule2/rule3를 살리는 지점이자 E2E 관측 경로.
3. **reconcile "루프" = reconcile-on-report.** 별도 타이머 없이 5분마다 도착하는 UsageReport가 actual 권위.
   수신 시점에 desired(assignments) vs actual 대조. 두 번째 스케줄러를 만들지 않는다.
4. **AMA 단일 "엔진 락"(R3).** scheduler 틱(`auto --once`)과 command 핸들러(deliver 크리티컬 섹션)는 같은
   tsamx 풀을 만지므로 모든 bridge 변경 시퀀스를 하나의 mutex로 직렬화. P3 최대 동시성 난제.
5. **세션 시작 시 `switch_mode`+정책 무조건 재천명.** AMA switchMode/정책은 메모리 전용(재부팅 소실)이라
   재천명 없으면 재시작 에이전트가 zero-value=MANUAL로 떨어져 자동 스위칭 영구 정지. SessionSetup→
   SetSwitchMode→SetPolicy 순 인라인 하달(applied 게이트 제외).
6. **자동 스위칭 감지 이중.** `auto --once` exit code(0=switched/2=no-op/3=all-exhausted) 1차 신호,
   틱 전후 `status --json` 활성계정 비교로 from/to 신원 확보, `autoswitch_state.json` fsnotify로
   활성 불변 quarantine 변화까지 포착.
7. **reconcile 자동 교정은 좁게 게이트.** 드리프트 감지+경보는 항상, 자동 재하달은 안전·멱등 케이스
   (배정됐는데 로컬 부재→재deliver / detached인데 로컬 존재→재recall)만, 루프 방지 카운터와 함께.
   전면 자동 교정은 P3 범위 밖.
8. **AccountEvent는 Outbox 경유, usage 리포트는 비경유.** 이벤트는 dedupe 아웃박스 + 재연결 flush로
   재전송하나, **전달은 best-effort**다 — Outbox가 메모리 전용이고 transport가 fire-and-forget(sendCh
   적재 성공을 전송으로 간주)이라, 프로세스 재시작 또는 적재 직후 스트림 단절 시 인플라이트 이벤트 1건이
   유실될 수 있다. **권위 상태는 이벤트가 아니라 5분 UsageReport + reconcile-on-report**이므로 상태 정합은
   자가치유되고 잃는 것은 감사용 알림뿐이다(R3 리뷰 A·B 공통, ADVERSARY H4 확인). 무손실 전달(transport
   ack 기반)은 후속 하드닝 이월. usage 스냅샷은 다음 틱이 대체하므로 두절 시 드롭 → 전송 블로킹 위험 제거.
9. **E2E는 P2 하네스 확장** — `cache/usage.json` 프리시드로 활성계정 ≥임계 유도, 실 `auto --once` 구동,
   AMS의 AccountEvent(switch) 수신 + 활성계정 전환 + usage_snapshots 행 검증.

## O4-C 하이브리드 (확정)

### proto 변경 — `SetPolicy` 신설 (cmd 17)
mode(로테이션 on/off)와 policy(threshold/strategy)는 독립 축이라 SetSwitchMode 확장보다 신규 메시지 채택.
`reserved 17-29` 주석이 이미 예고. (P3 시점 O4-C: cooldown/hysteresis 미포함. **P5 F4에서 필드 3·4로 추가돼 O4-B로 완성** — 아래 SetPolicy는 P3 당시 형태.)

```proto
// AmsCommand.oneof 에 추가, reserved 를 18-29 로 축소
SetPolicy set_policy = 17;
reserved 18 to 29;

message SetPolicy {
  double threshold_pct = 1;                       // tsamx autoswitch.threshold 주입; 0 = 로컬 기본 유지
  SwitchNow.SwitchStrategy default_strategy = 2;  // auto/switch_now 기본; UNSPECIFIED = 로컬 유지
  // cooldown/hysteresis 의도적 부재 — tsamx 로컬 (O4-C)
}
```
`SwitchNow.SwitchStrategy`(BEST/NEXT_AVAILABLE) 재사용, 신규 enum 불요. **wire 호환**: 신규 메시지 +
reserved(미사용) 번호를 oneof arm으로 승격 = 순수 additive → buf breaking 통과.

### AMS 배선
- **저장**: `servers`에 `threshold_pct FLOAT NULL` + `default_strategy TEXT NULL` (alembic 0003). NULL = 하달 안 함(로컬 유지).
- **하달 시점**: 결정5 세션 재천명에 SetPolicy **무조건 포함** — SessionSetup→SetSwitchMode→SetPolicy 순.
- **REST 변경 경로**: `:switch-mode`/정책 PATCH가 컬럼 갱신 + 연결 중이면 즉시 재하달. 아웃박스 재사용
  (`agent_commands.command_type="set_policy"`, `assignment_id` NULL — 이미 nullable). `_build_command`에 set_policy 분기 + 서명.

### AMA 배선
- `handleSetPolicy`: **엔진 락 경유**(threshold 변경이 진행 중 틱 판정 기준을 바꾸므로 직렬화 필수) →
  `tsamx config set autoswitch.threshold <pct>`(pct>0일 때만) + `default_strategy`를 Handler 메모리에 저장해
  scheduler `auto --once`/`switch_now(strategy 미지정)` 기본값으로 사용.
- **로컬 영속 불요**: 재부팅 후 세션 재천명으로 복구 → 사이드카 기록 불필요. 재천명 도착 전 첫 틱은 기존
  로컬 threshold로 동작(허용, 곧 덮임). set_mode와 동일 applied 게이트 비적용(재천명 멱등).

## 1. usage 인제스트 배선 (선행)
- **AMA**: `cmd/ama`에 reporter 5분 ticker 고루틴. `BuildUsageReport(SCHEDULE)` → 연결 시 non-blocking Send,
  미연결 시 드롭(usage는 이벤트 아님). Register.accounts는 연결 직후 `tsamx list --json`로 채워 즉시 reconcile
  가능하게(KEK 없이도 list는 읽힘). switch_mode도 Register에 실어 AMS 재천명과 교차확인.
- **AMS**: `_store_usage`가 저장 후 `reconcile.reconcile_from_report(db, tenant_id, server_id, report)` 호출.
  이 함수가 rule2(첫 UsageReport를 실상 권위로) + rule3(actual에 계정 존재 시에만 deliver 억제)를 실제 발화.

## 2. scheduler 틱 (§6.4)
신설 `internal/scheduler`. mode=auto일 때만 적응주기(기본 60s):
```
tick:
  engineLock.Lock()
  before ← bridge.Status(); code ← bridge.AutoOnce(); after ← bridge.Status()
  engineLock.Unlock()
  if code==0 || before.active≠after.active → Outbox.Enqueue(switch event, trigger=at-limit)
  if code==3 || poolSummary.allExhausted   → Outbox.Enqueue(all_exhausted, critical)
  fsnotify(autoswitch_state.json) → quarantine 변화 시 Enqueue(quarantine event)
```
- SetSwitchMode(auto→틱 시작 / manual→중지)가 scheduler 제어. `handleSetMode`를 start/stop 연동으로 확장.
- 두절 중에도 틱 계속, 이벤트는 Outbox → 재연결 OnConnect에서 `Outbox.Flush`(현재 미배선 — P3에서 배선).

## 3. switch_now + strategy
- `handleSwitchNow`의 strategy 분기 구현. bridge에 `SwitchStrategy(ctx, "best"|"next-available")` 추가
  (`tsamx switch --strategy best`). 엔진 락 경유. 성공 시 switch AccountEvent(trigger=manual) + `last_switched_at` 갱신
  (비상태 명령, 배정 전이 없음).
- reconcile는 manual 스위칭 활성계정 변화를 드리프트로 오판 금지 — is_current는 대조 대상 아님, allocation_status만 대조.

## 4. 소진 이벤트
- reporter `allExhausted` 재사용. scheduler 틱/리포트에서 감지 → 크리티컬 AccountEvent(all_exhausted).
- **AMS 훅**: 경보 기록 + `on_all_exhausted(server)` 확장점(P3는 no-op 스텁 + 로그). 자유계정 자동 배정은
  선정 정책 필요 → P3 범위 밖 명시.

## 5. AMS reconcile 루프 (reconcile-on-report)
- `reconcile_from_report`: 각 보고 계정 allocation_status를 assignment.state와 대조. 불일치 → usage_snapshots
  drift 마킹 + 경보. 좁은 자동 교정(결정7)은 `agent_commands`에 교정 명령 INSERT(아웃박스 재사용). 루프 방지:
  같은 (assignment, 교정유형) 재하달 횟수 상한.
- P2 단발 `apply_ack`는 유지, 리포트 기반 대조를 **추가**(대체 아님).

## 6. 구현 순서·트랙 (R3 표시)
- **선행 M1**: usage 인제스트(AMA ticker + AMS reconcile_from_report). E2E 관측 성립.
- **트랙 병렬**: T-AMA(scheduler+switch_now+events+outbox flush+handleSetPolicy) ∥
  T-AMS(reconcile-on-report+SetPolicy 하달·servers 컬럼+REST 4스텁 배선: :switch-now/:switch-mode/:refresh-usage/:recover) ∥
  T-공통(switch_mode+정책 세션 재천명, proto SetPolicy 추가·재생성).
- **R3 지점**: (a) AMA 엔진 락 직렬화, (b) switch_now/auto 외부효과, (c) reconcile 자동 교정 루프 방지, (d) SetPolicy 하달 경로.
- 마일스톤: M2 mode=auto+정책 세션 재천명 왕복 → M3 단일 서버 threshold 하달→95%→switch event 수신 →
  M4 all-exhausted → M5 10계정 E2E.

## 7. 테스트 전략
P2 docker-compose E2E 확장. threshold=90 하달 → 활성계정 91% 프리시드(`cache/usage.json` fiveHour.pct) →
mode=auto 세션 → 실 `auto --once` 틱 1회 강제(테스트 훅) → 검증: (1) AMS가 AccountEvent{kind=switch,
trigger=at-limit} 수신, (2) `tsamx status --json` 활성계정 전환, (3) usage_snapshots에 switch_event 행.
회귀: 전계정 91%→all_exhausted 이벤트, manual 모드에서 틱 미발생, deliver 크리티컬 섹션 중 틱 차단(엔진 락),
default_strategy=BEST 하달 후 switch_now(미지정)가 best로 동작.

## 8. 미해결·위험
- **P3에서 처리**: deliver×scheduler 경합(엔진 락), switch_mode+정책 세션 재천명, Outbox flush 배선.
- **P3 이월 유지**: O5 러너 무중단/오과금(배포), recall 이전활성 복귀 후 stranded, sent-미ack 고착
  (reconcile-on-report가 부분 완화 — actual 부재 시 재하달 억제 해제로 재시도 유발, 완전한 타임아웃 재시도는 별도).
- **위험**: reconcile 자동 교정 피드백 루프 — 반드시 좁은 게이트+상한. 리뷰 필수.

## SSOT 반영 (이 브랜치에서 완료)
- §8 O4 = 하이브리드(O4-C) 확정.
- §6.4 = SetPolicy 하달·세션 재천명·reconcile-on-report·엔진 락 명문화.
