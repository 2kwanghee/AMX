# 계정 풀 API 계약 (서버·웹 공통 기준)

서버와 웹이 같은 기준으로 구현한 뒤 확정한 문서다. 배경은 `account-pool-automation-plan.md`, 실제 서버 스키마는 `ams-server/app/schemas.py`의 Pool* 모델, 웹 타입은 `ams-web/src/lib/api-client/types.ts`가 정본이다. 세 곳이 어긋나면 서버 스키마를 진실로 본다.

모든 경로는 기존 관례를 따른다. `/api/v1/tenants/{tenantId}/...`이고 JSON은 camelCase(pydantic to_camel 별칭)다. 웹은 BFF 허용목록(`ams-web/src/lib/server/upstream.ts`)에 같은 모양을 정규식으로 등록해야 통과한다.

## 열거값

- poolState: `ready`, `leased`, `recalling`, `cooling`, `pinned`, `held`
- poolPolicy.mode: `manual`, `auto`
- recommendation.kind: `prefetch`(다음 계정을 미리 deliver), `swap`(switch_now 후 이전 계정 recall), `recall_idle`(target_leases 초과분 회수), `lease`(빈 서버에 배정)
- chain.step: `deliver`, `switch`, `recall`, `done`, `failed`
- chain.kind: recommendation.kind와 같은 값. 체인 종류를 from·to만으로는 되돌릴 수 없어(prefetch와 swap이 같은 모양) 서버가 함께 싣는다.
- ineligibleReason: `api_key`, `excluded`, `unusable`, `pinned`, `held`, `no_observation`. 적격이면 null.
- poolEvent.kind: `state_changed`, `recommendation_created`, `recommendation_dropped`, `chain_started`, `chain_step`, `chain_done`, `chain_failed`, `policy_changed`, `automation_paused`, `automation_resumed`

## 엔드포인트

| 메서드 | 경로 | 본문·질의 | 응답 |
|---|---|---|---|
| GET | `/pool` | | PoolOverview |
| PATCH | `/servers/{serverId}/pool-policy` | PoolPolicy(부분) | Server(poolPolicy 포함) |
| POST | `/accounts/{accountId}/pool:pin` | | Account |
| POST | `/accounts/{accountId}/pool:unpin` | | Account (ready로) |
| POST | `/accounts/{accountId}/pool:hold` | | Account |
| POST | `/accounts/{accountId}/pool:release` | | Account (held·cooling을 ready로 강제) |
| GET | `/pool/recommendations` | | Recommendation[] |
| POST | `/pool/recommendations/{id}:apply` | | Chain |
| GET | `/pool/chains` | `status=active\|all` | Chain[] |
| POST | `/pool/chains/{id}:ack` | | Chain (실패 체인 확인, 자동 실행 재개) |
| POST | `/pool:pause` | | { automationPaused } |
| POST | `/pool:resume` | | { automationPaused } |
| GET | `/pool/events` | `limit=100` | PoolEvent[] |

`status=active`는 도는 체인(deliver·switch·recall)만, `all`은 끝난 것과 실패까지 최신순으로 준다. 실패 체인은 확인(:ack) 전까지 그 서버의 자동 실행을 막는다.

## 스키마

- PoolPolicy { mode, targetLeases:int=1(1~5), swapAtPct:int=85, prefetchAtPct:int=70, minLeaseMinutes:int=30(0~1440), readyReturnPct:int=20 }
- WindowState { windowId:string(five_hour·seven_day 등), pct:number|null, resetsAt:datetime|null, usageFetchedAt:datetime|null, reportedAt:datetime, serverId:uuid }
  - pct가 null인 창은 관측을 못 읽은 것이다. 0으로 채우면 화면이 "여유 100%"로 오독하므로 미상은 미상으로 둔다. 콘솔은 이 창에 막대를 그리지 않고 "미상"만 적는다.
- PoolAccount { accountId, email, provider, poolState, coolingUntil:datetime|null, coolingWindowId:string|null, leasedServerId:uuid|null, leaseStartedAt:datetime|null, lastLeaseEndedAt:datetime|null, windows:WindowState[], poolStateChangedAt:datetime|null, autoEligible:bool, ineligibleReason:string|null }
  - autoEligible·ineligibleReason은 서버의 후보 필터(`services.pool.ineligible_reason`)와 같은 함수가 낸 값이다. 콘솔이 "왜 이 계정은 한 번도 안 뽑히지"에 화면에서 답하려고 싣는다. 담기는 것은 지속적 사유뿐이고, 대여·충전 같은 정상 국면은 상태 열이 이미 보여준다.
- PoolServer { serverId, name, status, poolPolicy, leasedAccountIds:uuid[], activeAccountId:uuid|null, inFlight:bool, maxPct:number|null }
- Recommendation { id, serverId, kind, fromAccountId:uuid|null, toAccountId:uuid|null, reason:string, createdAt, triggerPct:number|null }
- Chain { id, serverId, recommendationId:uuid|null, fromAccountId:uuid|null, toAccountId:uuid|null, kind, step, error:string|null, startedAt, updatedAt, ackedAt:datetime|null, actor:string }
  - ackedAt은 실패 체인을 운영자가 확인한 시각이다. 확인 전이면 null이다.
- PoolEvent { id, kind, accountId:uuid|null, serverId:uuid|null, detail:object, createdAt, actor:string }
  - actor는 `pool-controller` 또는 관리자 이메일이다.
- PoolOverview { automationPaused:bool, accounts:PoolAccount[], servers:PoolServer[], recommendations:Recommendation[] }

## 상태 규칙 (서버가 30초 스윕에서 계산)

- 라이브 배정(비-detached) 보유면 leased(배정 state가 recalling이면 recalling).
- 배정 없음이고 어느 창 pct가 swapAtPct(서버 정책 없으면 85) 이상이며 resetsAt이 미래면 cooling. coolingUntil은 소진된 창들의 resetsAt 최댓값, coolingWindowId는 그 창.
- cooling이고 now가 coolingUntil 이상이며 최신 관측 pct가 readyReturnPct 이하면 ready. 관측이 없으면 coolingUntil에서 관측 유예(기본 15분)가 지난 뒤 ready.
- pinned·held는 운영자 설정값이라 스윕이 덮어쓰지 않는다.
- assignmentExcluded=true 계정은 풀 후보에서 빠진다(상태는 표시한다).

## 오류 코드 (problem+json의 code)

전이·경합 거부는 아래 코드로 온다. 웹은 `ams-web/src/lib/api-client/client.ts`에서 다음 행동을 알 수 있는 한국어 문장으로 바꾼다.

- `pool.state_conflict` 지금 상태에서 적용할 수 없는 계정 동작
- `pool.recommendation_stale` 이미 실행됐거나 조건이 해소된 권고
- `pool.chain_active` 그 서버에 이미 진행 중인 체인이 있음
- `pool.server_in_flight` 전달·회수가 수렴 중인 배정이 있어 새 체인을 걸 수 없음
- `pool.recommendation_invalid` 권고에 필요한 계정 정보가 비어 있음
- `pool.swap_target_not_installed` 교체 대상 할당이 아직 전달 중
- `pool.swap_target_elsewhere` 교체 대상 계정이 다른 서버에 할당됨
- `pool.recall_source_missing` 회수할 계정이 그 서버에 없음
- `pool.chain_not_failed` 확인 처리는 실패한 체인에만 가능
