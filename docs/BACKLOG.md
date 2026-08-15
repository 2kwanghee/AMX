# AMX 백로그 — 이월·미해결 항목 종합

> P0~P4 각 PR·리뷰에서 이월한 항목과 §8 미해결 결정을 한 곳에 정리한 운영 백로그.
> 각 항목은 **출처 Phase · 심각도 · 처리 시점/의존성**을 명시한다. §8(설계 결정)과 상호참조.
> 최종 갱신: 2026-08-08 (P4 병합 직후).

## 진행 현황
P0 계약 · P1 인벤토리 · P2 채널 · P3 스위칭 · P4 콘솔 = **완료·병합**. P5 SaaS = 미착수(장기).
아래는 완료판정을 막지 않아 이월된 항목들이다 — 어느 것도 병합된 기능을 되돌리지 않는다.

---

## A. 사용자 결정 대기 (구현 전 판단 필요)

| # | 항목 | 출처 | 내용 | 의존성 |
|---|---|---|---|---|
| A1 | ~~credential 역동기화~~ **구현 완료** | §8 O9 · §5.7 · P2 | proto `CredentialUpdate`(AmaMessage 15) + AMA `internal/resync/` + AMS `_apply_cred_update` + `credential_observed_at` 단조 래칫(설계의 credential_version 대체, §5.7) + E2E `test_o9_resync_e2e.py`로 **구현·병합 완료**(p2b-cred-resync). 2026-08-09 소급 확인 — 이 행이 stale였음. 잔여: 가용성 격리 패치(encrypt 실패 시 스트림 유지, 진행 중) | ✅ 완료 |
| A2 | **O8 ClickEye 연동 형태** | §8 O8 · P4 | P4에서 건너뜀. ClickEye가 AMS 조회 API를 읽는 방식·범위. 권장 = 신규 read-only 엔드포인트 + ClickEye 전용 API 키(관리자 토큰과 분리) | ClickEye 요구 확정 시 |
| A3 | **O3 API-key 계정 관리 포함 범위** | §8 O3 · P1 | P1에서 api_key 계정은 POST accounts 암호화 경로로 저장 가능하게 구현됨. 다만 구독 쿼터가 없어 95% 임계가 무의미 — 스위칭 풀에 포함할지 정책 미확정 | P3 스위칭 정책과 연동 |

## B. 배포 설계 (운영 배포 시 필수)

| # | 항목 | 출처 | 내용 | 시점 |
|---|---|---|---|---|
| B1 | **O5 러너 무중단 / deliver 오과금** | §8 O5 · P2·P3 | deliver 크리티컬 섹션(§6.3) 동안 `~/.claude/.credentials.json`이 순간 신규 계정으로 바뀜 → 그 창에 러너(Claude Code)가 요청 시 오과금. 러너 일시정지/파일락 + 같은 `~/.claude` 공유 보장 | 배포 설계 |
| B2 | **O10 tsamx 설치 인증 (D11 파급)** | §8 O10 · P2 | 프라이빗 레포 git 설치 서버측 인증. 1차 읽기전용 deploy key → 서버 증가 시 서버별 키/machine user → AMS wheel 아티팩트 서빙으로 GitHub 의존 제거. 절차: `DEPLOYMENT-TSAMX.md` | 배포 설계 |
| B3 | **O6 tsamx 업스트림 동기화 절차** | §8 O6 · P1 | claude-swap 업스트림 갱신을 `vendor/claude-swap-upstream` 3-way 비교로 수동 병합. CLI/JSON 호환성 체크리스트 + 소유자 지정. 절차: `UPSTREAM-SYNC.md` | 운영 |
| B4 | **TLS 종단 (D9)** | P2 | gRPC 서버는 현재 cert/key 제공 시 `add_secure_port`, 미제공 시 `AMX_GRPC_ALLOW_INSECURE=1` opt-in fail-closed. 실배포 cert/CA 발급·mTLS 구성 | 배포 설계 |

## C. 보안·복원력 하드닝

| # | 항목 | 출처 | 심각도 | 내용 |
|---|---|---|---|---|
| C1 | ~~AccountEvent 전달 무손실화~~ **대부분 기구현** | P3 결정8 · 리뷰 A·B | 중→하 | "메모리 전용"은 stale — 디스크 outbox 기구현: W1(재시작 시 `outbox.log` 리로드)·W2(전송 확인 후에만 del 톰스톤), `reporter/outboxlog.go`+`outbox_disk_test.go`. 2026-08-09 소급 확인(stale 4번째 사례). 잔존 창 = stream.Send 성공 후 AMS 커밋 전 crash(중복/유실)뿐 — 결정8 수용, 완전 무손실은 앱레벨 ack 재설계(G12·G30과 통합, 별도) |
| C2 | ~~UnwrapKEK per-agent 래핑~~ **구현 완료** | P2 | 중 | 세션 KEK를 에이전트별 ephemeral X25519 **NaCl sealed box**로 봉인(c2-kek-wrap 병합, `crypto.go:199-214` UnwrapKEK가 raw KEK·타 키 봉인·변조 거부, §7 In-transit 반영). 2026-08-09 소급 확인 — 이 행이 stale였음 |
| C3 | **BFF allowlist %2f 우회** | P4 ADVERSARY | 낮(하드닝) | P4에서 %2f/%5c 명시 거부 + 디코드 후 검사로 **처리 완료**. 향후 allowlisted prefix 아래 라우트 추가 시 재검토 |

## D. 회복 엣지 (P3 reconcile로 부분 완화, 완전한 처리는 후속)

| # | 항목 | 출처 | 내용 |
|---|---|---|---|
| D1 | **recall 실패(DIVERGED/REJECTED) stranded** | P2·P3 리뷰 A | 실패한 recall이 어떤 재요청도 못 받고 영구 stranded. 설계상 recall 실패 회복 미정의 |
| D2 | **sent-미ack 명령 고착** | P2 리뷰 A | 에이전트가 수신 후 ack 전 끊기면 명령이 "sent"로 고착, 배정 "delivering" 고착. reconcile-on-report가 actual 부재 시 재하달 억제 해제로 부분 완화되나, 완전한 타임아웃 재시도는 별도 |

## E. 콘솔·기능 갭 (P4)

| # | 항목 | 출처 | 내용 |
|---|---|---|---|
| E1 | **set-policy UI 어포던스** | P4 리뷰 B | 백엔드·BFF는 threshold/strategy 편집 지원, ams-web UI에 편집 버튼 없음(서버 PATCH 자체가 UI 부재). 완료판정은 BFF 레벨이라 비차단 |
| E2 | **events 엔드포인트 UI 소비자** | P4 | `GET …/servers/{sid}/events`는 BFF allowlist에 있으나 UI 소비자 없음 |
| E3 | ~~Principal 훅~~ **구현 완료** | P4 리뷰 B | P5 S1에서 `require_admin`→`Principal` 반환형 리팩터 구현·병합 완료(`app/api/deps.py`, F1 RBAC이 이 위에 구축됨). 2026-08-09 소급 확인 — 이 행이 stale였음 |

## F. P5 SaaS 준비 (장기)

| # | 항목 | 출처 |
|---|---|---|
| F1 | 테넌트 RBAC (현재 단일 관리자, Principal 훅 E3 선행) | 로드맵 P5 |
| F2 | 봉투암호화 (테넌트별 DEK를 KMS KEK로) | §7 · P5 |
| F3 | **O7 다중 AMS 인스턴스** — `_online` 인프로세스 레지스트리 → 공유 저장/내부 라우팅 | §8 O7 |
| F4 | **O4-B 전체 정책 중앙화** — SetPolicy에 cooldown/hysteresis 필드 추가 | §8 O4 |
| F5 | ~~과금 훅~~ **완료** — `billing_events` outbox(usage_snapshots 원장 → 테넌트×닫힌 UTC 일 집계, 멱등 스윕 락 …03, REST list/export). 내부 청구 스키마, 외부 결제 미연동·proto 무변경. | 로드맵 P5 |

## G. 정리·nit (R0, 완료조건 무관)

| # | 항목 | 출처 |
|---|---|---|
| G1 | `servers.status = degraded` 미정의 — 정의(반복 명령실패/부분 드리프트) 또는 enum 제거 | P4 리뷰 |
| G2 | ams-web servers 정책 PATCH 이중커밋 (트랜잭션 경계 냄새) | P3 리뷰 |
| G3 | 미설정 threshold 시 reporter `SwitchThresholdPct=95` vs 하달값 불일치 가능 | P3 리뷰 |
| G4 | §6.5 trigger 표기 불일치 (`at-limit` 하이픈 vs `ams_query` 언더스코어) | P0 발견물 |
| G5 | `:recover`가 501인데 §5.3은 P2 표기 — 문서-구현 정합 | P2·P3 리뷰 |
| G6 | alert ack/open_alert 좁은 레이스 (acked+open 잠깐 공존, 다음 auto-resolve가 소거) | P4 리뷰 A |
| G7 | ams-web `@amx/contracts`(gRPC proto codec) 미사용, REST DTO는 openapi 미러 | P4 |
| G8 | AMA `store.FindByEmail` 중복 email 시 맵 순회 순서 의존(비결정) — 동일 email 2배정 시 잘못된 ams_account_id 스탬프 가능(단일 AMA 희소) | B1 리뷰 B |
| G9 | deliver 재전송 no-op이 멱등 단축보다 DeliverLock 획득이 앞서 최대 5s(fail-open) 대기 — 정확성 무영향, 효율만 | B1 리뷰 B |
| G10 | 래퍼(`amx-claude`) 미경유 직접 `claude` 실행 시 deliver sub-second 창 잔존 — 배포에서 alias/webhook 진입점 강제 필요(O5 배포 경계) | B1 ADVERSARY |
| G11 | reconcile 재recall 인플라이트(CORRECTION_RECALL 큐잉, `pending_command_id` 미설정) 중 REST `:recall`이 중복 recall 명령 발행 가능 — recall 멱등이라 상태 무손상, 저심각 | 회복 리뷰 B |
| G12 | C1 완전무손실(b2): AMS 앱-ack + AccountEvent event_id dedup(proto 변경) — 현재 stream.Send 성공 후 AMS 커밋 전 crash의 잔존창(audit 중복/유실)은 결정8 수용 | 회복 설계 이월 |
| G13 | quarantine 경보가 event-only — `alerts.sync_from_report`에 리포트의 quarantined 상태 추가하면 C1 잔존손실에도 가시성 유지(소규모 인접 하드닝) | 회복 설계 이월 |
| G14 | 로그인 타이밍 오라클 — 미존재 email은 bcrypt 스킵으로 존재 여부가 타이밍에 노출(자격증명 물질 노출은 없음). dummy bcrypt로 균일화 가능 | F1 리뷰 A·ADVERSARY (SaaS) |
| G15 | `require_admin`이 세션 인증 요청당 DB 세션 2개 오픈 + `resolve_session` JOIN 후 admin 재조회(중복 1쿼리) — 효율, 기능 정상 | F1 리뷰 A·B |
| G16 | `mask_secret` 4hex(16bit) 교차 테넌트 상관 핸들 — 자격증명 노출 아님, 수용 명시 | F1 ADVERSARY (SaaS) |
| G17 | 자기 비활성 미방지 — global-admin이 자신을 disable/delete 가능(마지막 1인 rail만 방어). `Principal`에 admin_id 없어 요청자 식별 불가 → S2a 세션 로직 확장 필요. 루트 토큰이 최종 탈출구라 lockout 불가 | F1 S2b/c 리뷰 B |
| G18 | `ams-web` `verifyNav` dead code(집행은 ams-server 전담) + `create_admin` TOCTOU 오류코드 오표기(동시 tenant 삭제 시 FK위반이 duplicate_email로, 정상경로는 has_admins 가드가 차단, 협소) | F1 S2b/c 리뷰 |
| G19 | F2 KMS 어댑터(aws-kms/vault) 미구현 — 벤더 미정 스텁, provider_id/key_id 배관 완비. **local→KMS 혼재 시 기존 DEK 재래핑 스크립트 선행 필요**(신규 DEK만 provider swap) | F2 (사용자 KMS 결정 대기) |
| G20 | F2 로컬 KEK MVP는 단일 env 시크릿 — 격리·구조·KMS-ready는 즉시 이득이나 기밀 강화는 실 KMS 도입 시(§7 정직성) | F2 |
| G21 | F2 저심각: create_tenant 2-commit 비원자(DEK 실패 시 fail-loud) · O9 encrypt_secret try/except 밖(DEK 부재 시 스트림 드롭, 가용성 엣지) · `crypto._aad`·`kek._tenant_aad` 중복정의(DRY) | F2 리뷰 B |
| G22 | F3 claim-before-write 지연 완화: 재연결 write 실패 시 해당 command_id(및 미전송 배치 tail)를 `sent→queued` 즉시 리셋하면 90s D2 지연 제거. 단 **되돌림-후-재경쟁**(되돌린 명령을 타 인스턴스가 fetch, 멱등이라 무해)이라는 새 동시성 고려 필요 → 별도 설계·검토. 현재는 지연만(정확성 무해) | F3 리뷰 A·B |
| G23 | F3 문서: 스위퍼 "exactly one per tick"은 과장(실제 동시 배제·시차 중복 가능하나 멱등) · `alerts.sweep_offline` docstring "caller commits" 실제 내부 commit과 불일치 | F3 리뷰 B |
| G24 | **크로스-컴포넌트 계약은 유닛 목으로 못 잡음** — F1 로그인 502(BFF snake vs 서버 camel)가 유닛 목 자기완결로 통과했다가 실 e2e에서만 노출. **각 병합 시 전체 e2e 게이트 필수**(프로세스) | 로그인 hotfix 교훈 |
| G25 | F5 테넌트 삭제 시 청구 원장 소실 — `billing_events.tenant_id` FK가 CASCADE라 미export(pending) 원장이 조용히 소멸. `delete_tenant` 가드 체인(inventory.py)에 pending billing 검사 추가 또는 DEK처럼 RESTRICT 검토 | F5 리뷰 B |
| G26 | F5 export 후 정정 수단 부재 — void/재집계 API 없음, `ON CONFLICT DO NOTHING`이라 재스윕으로도 못 덮음(정정은 수동 SQL뿐). 내부 청구 정정 플로우는 과금 대상 확정 시 설계 | F5 리뷰 B |
| G27 | F5 시계 앞점프(VM resume/NTP step) 시 watermark가 미래로 전진해 그 구간 스냅샷 영구 미청구 가능(되감김은 안전). `reported_at < watermark` 도착 행 카운터·경보 없어 누락이 조용함 → **해소**: 스킵 카운터 로그 + `billing_watermark_future` 경보(#71), 그리고 rollup·billing 스윕이 주차(`start > last_closed_end`)를 감지하면 커서를 `last_closed_end`로 되감아 자가치유(다음 틱부터 정상 재정산 재개, rollup 멱등·billing 앵커로 이중계산 없음) | F5 리뷰 B |
| G28 | F5 저심각: 첫 실행/장기 다운 후 전 구간 usage 행 `.all()` 일괄 로드(일 단위 chunk 권장) · `_try_advisory_xact_lock` grpc/server.py·billing.py 중복 정의(DRY) | F5 리뷰 A·B |
| G29 | **A1 업스트림 자원 소모(중)** — cred_update 경로에 rate limit·복호 평문 길이 상한 전무. 인증 에이전트 1대로 ~4MB push 반복(테이블 블로트)·observed_at 마이크로초 증가 무제한 수락·거부 경로 로그/DB조회 증폭, to_thread 풀 고갈 파급. 내재화(자사 서버만)에선 위협 낮음 — 외부 노출 전 필수. 권고: 세션당 리밋 + 길이 상한 | A1 ADVERSARY 소급 |
| G30 | **A1 baseline 무ack 전진(중, 가용성)** — AMA가 전송 수락만으로 baseline 전진, AMS는 "not newer"를 조용히 무시 → 에이전트 시계 앞선 1회 push 후 보정 창(≤5분)의 정상 갱신이 재시도 없이 유실, 다음 회전까지 AMS 사본 stale(재인증 폴백으로 정합성은 유지). 권고: AMS 무시 시 AMA baseline 미전진(앱레벨 ack — C1/G12 ack 인프라와 공유 가능) | A1 ADVERSARY 소급 |
| G31 | A1 저심각 — `UpdateBaseline`이 재deliver 경합 창에서 구 평문으로 매니페스트 덮어씀(다음 tick 자가치유, fingerprint CAS 권고) · `del plaintext`는 메모리 소거 아님(docstring "wiped" 과장 정정) | A1 ADVERSARY 소급 |
| G32 | A1 가용성 격리의 이월 3건 — ① DEK 전면 장애 시 유실이 조용해짐(종전엔 세션 절단이 자기 고지) → `alerts.open_alert(kind="cred_resync_failed")` 관측 지점 권고 ② `except KekError` 협소: legacy Fernet 키 오설정(ValueError)·향후 KMS provider 예외는 미격리 ③ `_handle_upstream` 타 분기(usage/event/ack)도 디스패치 레벨 무격리 | A1 패치 리뷰 B |
| G33 | **openapi.yaml drift 일괄 해소** — `:recall` force/403(D1)·billing 경로(F5)·alerts 경로(P4)가 openapi 미반영. 계약 문서 일괄 동기화 필요 | D1 리뷰 B · F5 · P4 |
| G34 | D1 이월 — force recall 감사 기록 부재(감사 테이블 자체 없음, 상용 전 검토) · alerts.py docstring recall_failed 미반영 · 0011 downgrade는 recall_failed 행 존재 시 CHECK 재생성 실패 가능 · stale ack이 pending_command_id/last_error clobber(D1 이전부터, deliver 포함 공통) · alerts.resolve tenant 필터 부재 · 경보 ack 후 재실패 미재부상(기존 설계) | D1 리뷰 A·B |
| G35 | D2 이월 — switch_now 최종 실패의 last_error 기록 vs `_revert_assignment_on_send_failure` docstring 문구 불일치(경미) · switch_now 실패에 account-scoped 경보를 여는 시맨틱 검토 여지(다음 명령 CONVERGED로 해소돼 결함 아님) | D2 리뷰 B |

---

## 우선순위 제안 (참고)
1. **실운영 배포 직전 필수**: B1(오과금) · B4(TLS) · C2(KEK 래핑) · A1(O9 실계정 판별).
2. **운영 안정화**: D1·D2(회복 엣지) · C1(이벤트 무손실).
3. **콘솔 완성도**: E1(정책 UI) · E2 · G1.
4. **SaaS 전환 시**: F1~F5(E3 선행).
5. **언제든 저비용**: G2~G7.
