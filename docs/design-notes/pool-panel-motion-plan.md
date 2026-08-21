# 계정 풀 화면 최종 기획: 권고 근거 노출과 상황판 모션

## 1. 배경

사용자 요구는 두 가지다.

1. 교체 권고의 기준을 어디서 바꾸는지 알 수 없다. 설정 화면이 없다고 느낀다.
2. 상황판이 정적이다. 카드가 레인을 옮기는 모습, 충전소에서 충전되는 모습, 남은 시간이 보이지 않는다.

현황 사실은 이렇다. 교체 권고 설정 화면은 `ams-web/src/components/PoolPanel.tsx`의 PolicyModal로 이미 있다. 문제는 진입점이 서버 정책 표의 행 클릭 하나뿐이라는 점이다. 권고 항목에는 종류 라벨, 서버명, pct만 있어 어느 임계에 걸려 권고가 떴는지 화면 어디에도 적혀 있지 않다. 상황판은 30초 폴링으로 갱신되며 이동 중 프레임은 없고 결과만 도착한다. 충전 진행률 계산에 필요한 poolStateChangedAt, coolingUntil과 서버별 정책값 poolPolicy는 응답에 이미 실려 있다.

심사 두 건은 승자가 갈렸다. 한쪽은 1안(권고 근거 노출)을, 다른 쪽은 3안(최소 변경안)을 골랐다. 두 심사가 공통으로 높게 평가한 요소는 1안의 A1(판정 근거 문장)과 A6(정책 기반 색 기준), 3안의 CoolingClock 국소 타이머와 done 상태, 단위 테스트다. 공통으로 낮게 본 요소는 2안의 FLIP 훅과 장식성 모션, 1안의 전역 env 상수 하드코딩, View Transition 의존이다. 그래서 1안을 뼈대로 삼되 레인 이동은 3안의 도착 하이라이트 방식으로 바꾸고 3안의 구현 안전장치를 전부 편입한다. 서버는 건드리지 않는다.

## 2. 최종 항목 표

| id | 내용 | 위치 | 규모 | 출처 |
|---|---|---|---|---|
| A1 | 권고 항목에 판정 근거 한 줄. "교체 임계 85% 이상 · 현재 91%" 형태. swap/prefetch는 poolPolicy와 r.triggerPct로 조합한다. lease와 recall_idle은 targetLeases 대 leasedAccountIds.length를 "기준" 수준으로만 적고 단정하지 않는다. 기존 r.reason은 아래 muted로 유지 | PoolPanel.tsx RecommendationList, 문구 조립은 pool.ts `recommendationBasis(r, policy, leasedCount)` 순수 함수 | S | 1안 A1 |
| A2 | 권고 항목의 "적용" 옆에 "기준 변경" 보조 버튼. 누르면 해당 서버 PolicyModal. RecommendationList에 onEditPolicy(serverId) prop 추가, PoolPanel에서 servers 검색 후 setPolicyOf | PoolPanel.tsx RecommendationList 버튼 영역과 호출부 | S | 1안 A2, 3안 A1 |
| A3 | 권고 섹션 제목 옆에 "서버별 기준은 서버 정책에서 바꿉니다" 링크. 클릭하면 서버 정책 표로 scrollIntoView. 빈 상태 문구를 "현재 기준을 넘은 서버가 없습니다. 기준은 서버 정책에서 바꿉니다."로 교체 | PoolPanel.tsx RecommendationList, ServerTable 최상위에 id="pool-server-policy" | S | 1안 A3, 3안 A2 |
| A4 | 서버 정책 표에 교체 임계, 미리 전달 임계 열 2개와 행 끝 "편집" 버튼. 행 클릭은 유지. 전역 기준 한 줄은 값 없이 "창 상한, 관측 유예, 관측 만료는 서버 환경변수에서 바꿉니다"로만 적는다 | PoolPanel.tsx ServerTable thead/tbody | S | 1안 A4 축소 |
| A5 | PolicyModal 제목을 "교체 기준 · {서버명}"으로. 입력 5개(교체 임계, 미리 전달 임계, 최소 대여 시간, 복귀 임계, 모드)에 한 줄 도움말. 상단에 "이 값이 권고 생성 기준입니다" 안내 | PoolPanel.tsx PolicyModal | S | 1안 A5, 3안 A3, 2안 A1 |
| A6 | 카드 막대 색 기준을 하드코딩 85/70에서 대여 서버 poolPolicy.swapAtPct/prefetchAtPct로 교체. 대여 중이 아닌 카드는 기본값 85/70을 쓰고 눈금선은 그리지 않는다. 대여중 카드에만 임계 눈금선 2개 | PoolPanel.tsx pctTone, PoolCard. globals.css .pool-win-mark | M | 1안 A6, 2안 B6 |
| B1 | 충전소 카드 본문을 CoolingClock 소컴포넌트로 분리. 안에서만 useNow(1000). 남은 시간 10분 이상이면 분 단위, 미만이면 mm:ss. 탭이 숨겨지면(document.visibilityState) 틱 정지. 아래에 충전 게이지. 진행률 = clamp((now - poolStateChangedAt)/(coolingUntil - poolStateChangedAt), 0, 1). 두 시각 중 하나라도 없거나 파싱 실패면 게이지 숨기고 텍스트만 | PoolPanel.tsx CoolingClock 신설, pool.ts `coolingProgress(a, now)` 신설, globals.css .pool-cool-gauge | M | 1안 B1, 3안 B2·B3, 2안 B5 |
| B2 | done 상태. now가 coolingUntil을 지났는데 서버가 아직 ready로 옮기지 않았으면 게이지 100% 고정, 맥동 정지, "복귀 대기" 표시. 카드는 서버 응답이 올 때까지 충전소에 둔다 | CoolingClock 내부 조건, globals.css .pool-cool-gauge.done | S | 3안 B6 |
| B3 | 레인 이동 표현. PoolPanel 최상위에 useRef로 이전 스냅샷 Map<accountId, poolState>를 들고 폴링 후 비교한다. poolState가 바뀐 카드에만 pool-card-enter 클래스를 한 번 붙인다(배경 accent 틴트에서 투명으로, translateY(-6px)에서 0으로). animationend에서 제거. 첫 마운트(prev undefined)는 건너뛴다. View Transition은 쓰지 않는다 | PoolPanel.tsx PoolPanel과 PoolCard, globals.css @keyframes pool-card-enter | M | 3안 B1, 1안 B2 폴백 경로 |
| B4 | 이동한 카드에 "대여중에서 이동 · 방금" 꼬리표를 POLL 길이만큼 표시. state_changed 최신 이벤트의 이유가 있으면 함께 적는다 | PoolCard 상단, pool.ts poolStateLabel 재사용 | S | 1안 B3 |
| B5 | 창 막대 width transition을 `--m-base`로, 새 권고 항목(이전 id 집합과 diffChanged)에 pool-card-enter 재사용 하이라이트, 요약 숫자 변화 시 짧은 색 변화. 롤링이나 숫자 틱은 없다. 강조는 "새로 넘은 순간"에만 붙이고 폴링마다 재점화하지 않는다 | globals.css .pool-win-fill, .pool-reco-item.new, .pool-stat b.changed. PoolPanel.tsx 이전 값 ref | S | 1안 B4, 3안 B7, 2안 B6 규칙 |
| B6 | topbar 새로고침 버튼 옆에 "마지막 갱신 n초 전". SWR error면 "갱신 실패 · 재시도 중" | PoolPanel.tsx pool-summary | S | 1안 B5 |
| B7 | 모션 토큰 3개와 이징 2개를 :root에 정의하고 pool-* 모션은 전부 이 토큰만 쓴다. reduced-motion 블록 하나에서 일괄 처리 | globals.css :root, pool 섹션 끝 | S | 2안 B1 축소 |
| C1 | 단위 테스트. coolingProgress(시각 누락, NaN, until < changedAt, now > until), recommendationBasis(종류 4개), diffChanged(동일 집합, 추가, 삭제) | ams-web/src/lib/pool.test.ts | S | 3안 C1 |

후속(서버 변경 필요, 이번 범위 밖)

| id | 내용 |
|---|---|
| F1 | GET /pool 응답에 전역 임계(window_high, observation_grace, window_stale)를 실어 A4 문구에 실제 값을 표시 |
| F2 | _desired_recommendation의 판정 근거를 구조화 필드로 응답에 추가해 A1의 lease/recall_idle 추정을 서버 값으로 대체 |

## 3. 모션 규격

토큰은 B7에서 정의한다.

| 토큰 | 값 | 용도 |
|---|---|---|
| --m-fast | 160ms | 색, 투명도, 테두리 |
| --m-base | 320ms | 막대 width, 게이지 채움 |
| --m-move | 600ms | 카드 진입 하이라이트(pool-card-enter), 권고 도착 |
| --ease-out | cubic-bezier(.2,.8,.2,1) | 진입, 하이라이트 |
| --ease-inout | cubic-bezier(.65,0,.35,1) | 막대, 게이지 |

색은 기존 토큰만 쓴다. 하이라이트는 accent-soft에서 surface로, 게이지 채움은 warn 톤, done 상태는 ok 톤, 임계 눈금선은 swap이 crit, prefetch가 warn이다. 게이지 맥동은 기존 live-pulse 또는 pipe-move keyframe을 재사용하고 새 keyframe은 pool-card-enter 하나만 추가한다. 반복 깜박임은 없고 모든 강조는 한 번만 친다. 상시 움직이는 요소는 게이지 맥동뿐이며 그것도 done 상태에서 멈춘다.

reduced-motion 동작은 한 블록에서 처리한다. pool-* transition-duration과 animation-duration을 0.01ms로 둔다. 0ms가 아니라 0.01ms인 이유는 animationend 이벤트가 발생해야 B3의 클래스 제거가 동작하기 때문이다. 맥동은 정지한다. 정보는 텍스트로 남긴다. 이동 카드에는 B4 꼬리표가 그대로 보이고, 게이지는 정적 채움과 퍼센트 숫자로, 임계 근접은 테두리 색만으로, 새 권고는 정적 테두리로 표현한다. A3의 scrollIntoView는 behavior auto로 바꾼다.

## 4. 범위 밖과 이유

- 전역 env 임계를 화면에서 바꾸는 기능. 읽기 API도 쓰기 API도 없다. 값을 프론트 상수로 박는 1안 A4 원안은 env 변경 시 틀린 값을 보여 주는 부채라서 빼고 F1 후속으로 넘긴다.
- View Transition API 기반 실제 위치 보간. 두 심사 모두 폴백이 본체가 된다고 봤고 병행 브랜치와의 충돌 비용도 크다. 30초 폴링에서는 도착 하이라이트와 꼬리표로 "바뀌었다"는 신호가 충분하다. 사용자에게 카드가 물리적으로 미끄러지는 모습은 나오지 않는다고 미리 알린다.
- 2안의 FLIP 훅, 체인 스텝 바, 숫자 슬롯 틱, 방향 잔광, border-pulse. 운영자 판단에 기여하지 않는 장식이고 신규 훅과 CSS 규칙을 가장 많이 늘린다.
- 폴링 주기 단축, SSE/WebSocket.
- ServersPanel PolicyModal 통합이나 재설계. 제목 변경(A5)으로 혼동만 줄인다.
- 이벤트 타임라인 모션, 카드 드래그.
- _desired_recommendation 판정 로직 변경.

## 5. 검증 방법

자동 검증은 ams-web에서 typecheck, lint, test, build를 순서대로 돌린다. C1 테스트가 전부 통과해야 한다. 기존 pool.test.ts의 통과 여부도 함께 본다.

수동 검증은 dev 환경에서 다음을 확인한다.

1. 권고 항목마다 근거 문장이 보이고 "기준 변경" 버튼이 해당 서버 PolicyModal을 연다. 모달 제목에 "교체 기준 · 서버명"이 뜬다.
2. 권고 0건일 때 새 빈 문구가 보이고 링크 클릭으로 서버 정책 표로 이동한다.
3. 서버 정책 표에 임계 열 2개와 편집 버튼이 있고, PolicyModal에서 swapAtPct를 80으로 낮추면 해당 서버 대여중 카드의 막대 색과 눈금선이 80 기준으로 바뀐다.
4. 충전소 카드에 게이지가 차오르고 10분 미만에서 mm:ss로 바뀐다. 탭을 숨겼다 돌아오면 시각이 건너뛰어 맞는다. coolingUntil이 지나면 100% 고정과 "복귀 대기"가 보이고 다음 폴링에 카드가 배급처로 옮겨진다.
5. 카드 이동 시 도착 카드에 하이라이트 한 번과 꼬리표가 보이고 다음 폴링에서 재점화되지 않는다. 첫 로드에는 하이라이트가 없다(StrictMode 포함).
6. 새 권고 도착 시 항목 하이라이트 한 번. 요약 숫자 색 변화 한 번.
7. "마지막 갱신 n초 전"이 매초 올라가고 서버를 내리면 "갱신 실패 · 재시도 중"으로 바뀐다.
8. OS에서 모션 줄이기를 켜고 1~7을 반복한다. 움직임은 없고 텍스트 정보는 전부 같은 자리에 남아야 한다. 하이라이트 클래스가 DOM에 남지 않는지 확인한다.

실패로 간주하는 조건은 게이지가 0 미만이나 100 초과로 그려지는 경우, 첫 마운트에 하이라이트가 붙는 경우, 모션 줄이기에서 꼬리표나 근거 문장이 사라지는 경우, 충전소 카드가 없는데 1초 타이머가 도는 경우다.
