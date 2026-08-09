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
| A1 | ~~O9 refresh 회전 판별~~ → **credential 역동기화 구현** | §8 O9 · §5.7 · P2 | **판별 완료(회전형 확정, 2026-08-08 `tools/o9_refresh_probe.py`)** + 방향 결정(역동기화 채택). 이제 **구현 항목**: AMA가 refresh 갱신본을 AMS로 역전송(신규 proto `CredentialUpdate`, AMA 감지, AMS `encrypted_secret` 갱신, credential_version 단조성). 크로스서버 재배정 자동화. **R3**(credential 흐름·서명·경합), P2 채널 확장 | 착수 대기 (사용자 우선순위) |
| A2 | **O8 ClickEye 연동 형태** | §8 O8 · P4 | P4에서 건너뜀. ClickEye가 AMS 조회 API를 읽는 방식·범위. 권장 = 신규 read-only 엔드포인트 + ClickEye 전용 API 키(관리자 토큰과 분리) | ClickEye 요구 확정 시 |
| A3 | **O3 API-key 계정 관리 포함 범위** | §8 O3 · P1 | P1에서 api_key 계정은 POST accounts 암호화 경로로 저장 가능하게 구현됨. 다만 구독 쿼터가 없어 95% 임계가 무의미 — 스위칭 풀에 포함할지 정책 미확정 | P3 스위칭 정책과 연동 |

## B. 배포 설계 (운영 배포 시 필수)

| # | 항목 | 출처 | 내용 | 시점 |
|---|---|---|---|---|
| B1 | **O5 러너 무중단 / deliver 오과금** | §8 O5 · P2·P3 | deliver 크리티컬 섹션(§6.3) 동안 `~/.claude/.credentials.json`이 순간 신규 계정으로 바뀜 → 그 창에 러너(Claude Code)가 요청 시 오과금. 러너 일시정지/파일락 + 같은 `~/.claude` 공유 보장 | 배포 설계 |
| B2 | **O10 tsamx 설치 인증 (D11 파급)** | §8 O10 · P2 | 프라이빗 레포 git 설치 서버측 인증. 1차 읽기전용 deploy key → 서버 증가 시 서버별 키/machine user → AMS wheel 아티팩트 서빙으로 GitHub 의존 제거 | 배포 설계 |
| B3 | **O6 tsamx 업스트림 동기화 절차** | §8 O6 · P1 | claude-swap 업스트림 갱신을 `vendor/claude-swap-upstream` 3-way 비교로 수동 병합. CLI/JSON 호환성 체크리스트 + 소유자 지정 | 운영 |
| B4 | **TLS 종단 (D9)** | P2 | gRPC 서버는 현재 cert/key 제공 시 `add_secure_port`, 미제공 시 `AMX_GRPC_ALLOW_INSECURE=1` opt-in fail-closed. 실배포 cert/CA 발급·mTLS 구성 | 배포 설계 |

## C. 보안·복원력 하드닝

| # | 항목 | 출처 | 심각도 | 내용 |
|---|---|---|---|---|
| C1 | **AccountEvent 전달 무손실화** | P3 결정8 · 리뷰 A·B | 중 | Outbox 메모리 전용 + transport fire-and-forget이라 프로세스 재시작/적재 직후 단절 시 인플라이트 이벤트 1건 유실 가능. 현재 상태 정합은 usage report reconcile로 자가치유(감사 알림만 손실). 무손실은 transport ack 기반 재설계 필요 |
| C2 | **UnwrapKEK per-agent 래핑** | P2 | 중 | 현재 WrappedKey는 원시 KEK passthrough, 전송 기밀성은 TLS(B4)에 위임. 프로덕션 전 per-agent transport key/KMS 래핑 필요(코드 TODO 명시) |
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
| E3 | **Principal 훅 미구현** | P4 리뷰 B | 설계 결정5는 `require_admin`→`Principal` 반환형 리팩터(P5 테넌트 스코핑 자리)를 요구했으나 미구현. 단일 관리자라 P5 강제 재작업은 아님 |

## F. P5 SaaS 준비 (장기)

| # | 항목 | 출처 |
|---|---|---|
| F1 | 테넌트 RBAC (현재 단일 관리자, Principal 훅 E3 선행) | 로드맵 P5 |
| F2 | 봉투암호화 (테넌트별 DEK를 KMS KEK로) | §7 · P5 |
| F3 | **O7 다중 AMS 인스턴스** — `_online` 인프로세스 레지스트리 → 공유 저장/내부 라우팅 | §8 O7 |
| F4 | **O4-B 전체 정책 중앙화** — SetPolicy에 cooldown/hysteresis 필드 추가 | §8 O4 |
| F5 | 과금 훅 | 로드맵 P5 |

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

---

## 우선순위 제안 (참고)
1. **실운영 배포 직전 필수**: B1(오과금) · B4(TLS) · C2(KEK 래핑) · A1(O9 실계정 판별).
2. **운영 안정화**: D1·D2(회복 엣지) · C1(이벤트 무손실).
3. **콘솔 완성도**: E1(정책 UI) · E2 · G1.
4. **SaaS 전환 시**: F1~F5(E3 선행).
5. **언제든 저비용**: G2~G7.
