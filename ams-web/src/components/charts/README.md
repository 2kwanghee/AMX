# 차트 프리미티브

대시보드 개편(`../../../../docs/design-notes/dashboard-redesign-plan.md` §4·§5)에 쓸 SVG
위젯 세트다. 차트 라이브러리는 안 쓰고 손수 SVG + CSS/rAF로 그리되, 계산만
`d3-shape`·`d3-interpolate`·`d3-sankey` 서브패키지를 빌린다(눈금 계산은 `d3-scale` 없이
`math.ts`의 `niceTicks`가 직접 nice-numbers 알고리즘으로 처리한다).

이번 PR은 프리미티브와 순수 함수·테스트까지다. 대시보드 화면에 실제로 배치하는
작업(`src/app/dashboard/page.tsx`)은 다음 PR 몫이라, 아래 props는 API 응답을 그대로
받는 게 아니라 컴포넌트가 필요로 하는 최소 형태로 따로 정의했다. `src/lib/api-client/types.ts`의
`Stats*` 타입과 필드명이 대부분 겹치므로 다음 PR에서 매핑은 대개 필드 이름만
바꾸는 수준이다.

## 공통 규칙

- 전부 `'use client'`. 색은 `globals.css`의 토큰(`--kpi-*`, `--ok/warn/crit`,
  `--accent`, `--muted`, `--border`, `--surface`)만 쓴다. 시리즈가 4개를 넘으면
  `palette.ts`의 `seriesColor()`가 같은 4색을 `color-mix`로 옅게 우려 재사용한다.
- 애니메이션은 `prefers-reduced-motion`이면 즉시 최종 상태로 멈춘다. 값 카운트업은
  `useCountUp`, path 보간은 `useInterpolatedPath`가 각각 내부에서 검사한다. CSS
  키프레임(히트맵 진입)은 `globals.css` 끝의 reduced-motion 블록에서 끈다.
- 마운트 시 렌더값은 항상 최종값이다(`useNow` 패턴과 동일하게 hydration mismatch를
  피한다). 애니메이션은 이후 갱신에서만 재생된다.
- 접근성: 인터랙티브 요소가 아닌 SVG는 `role="img"` + `aria-label`로 텍스트 대체를
  준다. 값이 없는 경우(빈 배열·합계 0 등)는 `.chart-empty` 플레이스홀더로 빠진다.

## 순수 함수 (`math.ts`)

애니메이션 타이밍이나 레이아웃 계산은 전부 여기 있고, `test/charts-math.test.ts`가
경계값(빈 배열, 단일 값, 합계 0, min===max, 음수 없음)을 검증한다.

- `easeOutCubic(t)`, `countUpValue(from,to,t)`, `countUpFrames(from,to,frames)` — 카운트업.
- `niceTicks(min,max,n)` — nice-numbers 알고리즘으로 눈금 배열.
- `formatCompact` — `usage-format.ts`의 `fmtTokens` 재노출(1.2K/3.4M).
- `stackSeries(series)` — 여러 시리즈를 누적 밴드([y0,y1] 쌍)로.
- `donutArcs(values, opts?)` — d3-shape arc 경로 문자열 + fraction + 중간 각도.
- `areaPath(values, w, h, maxY)` — 누적 밴드 하나를 area path 문자열로.
- `sankeyLayout(nodes, links, w, h)` — d3-sankey 래핑. 값 0·자기참조·존재하지 않는
  노드를 참조하는 링크는 미리 걸러낸다.
- `heatmapScale(max)` — 값을 0~1 강도로 정규화하는 함수를 만든다.
- `flipPositions(prevOrder, nextOrder)` — 순위 재정렬 시 항목별 이동 칸수.

## 컴포넌트

### `KpiTile`
라벨 + 카운트업 값 + 증감 화살표 + 스파크라인(기존 `common.tsx`의 `Sparkline`
재사용). `onClick`을 주면 버튼(탭 이동용), 없으면 정적 카드.

```ts
<KpiTile
  label="토큰"
  value={summary.tokens.value}
  prevValue={summary.tokens.prev}
  sparkline={summary.sparkline.tokens}
  tone="teal"
  onClick={() => onGo('usage')}
/>
```
`stats/summary`의 `tokens`·`sessions`·`alertsOpen`(각 `{value,prev}`)과 `cost`
(문자열이라 `valueText`로 넘긴다)에 대응한다.

### `AreaChart`
누적 영역 시계열. `stats/timeseries` 응답의 `buckets`(ISO 문자열)·`series`
(`{key,label,values}`, 이미 상위 8+other로 잘려서 온다는 전제)를 그대로 매핑한다.
호버하면 크로스헤어 + 버킷별 시리즈 값 툴팁.

### `Donut`
값 배열을 arc path로 그리는 도넛. 중앙은 기본으로 합계, 세그먼트를 호버하면 그
값·비율로 바뀐다. 범례는 별도 목록(색 구분이 어려운 경우의 텍스트 대체 겸용).
`stats/accounts`나 `stats/timeseries`(by=model)의 상위 항목을 `{key,label,value}`로
매핑해서 쓴다.

### `RankBars`
가로 순위 바. `rows`는 이미 내림차순 정렬된 것으로 기대한다(정렬은 API 책임).
기간이 바뀌어 순서가 바뀌면 FLIP으로 이전 위치에서 미끄러져 이동한다.
`stats/accounts` 행을 `{key: accountId, label: email, value: tokens}`로 매핑.

### `RingGauge`
원형 잔여 게이지. `pct`는 사용률(0~100, 클램프)이고 채움·색(70/90 임계)은 이
값 기준이다 — `account-remaining-usage-plan.md` 결정대로 게이지 자체는 사용률을
유지하고, 문구만 "잔여"로 뒤집는다. `remainingText`를 넘기면
`usage-format.ts`의 `fmtRemainingWindow()` 결과(리셋 시각 포함)를 그대로 쓸 수
있다. `stats/accounts`의 `remaining5HPct`/`remaining7DPct`(원래는 사용률 %)에 대응.

### `Heatmap`
요일×시간 세션 히트맵. `cells[요일][시간]`(요일 0=월요일, 시간 0~23) 2차원
배열을 그대로 받는다(`stats/heatmap` 응답과 동일 형태). 셀은 index*30ms(최대
300ms) 지연으로 순차 진입한다.

### `Sankey`
서버→계정 흐름. `nodes`(`{id,label,kind}`)·`links`(`{source,target,value}`)는
`stats/flows` 응답과 거의 동일하다(카멜케이스 그대로). 링크를 호버하면 그
경로만 강조하고 나머지는 흐려진다.

## 훅

- `useCountUp(value, ms=600)` — 숫자 값 rAF 보간.
- `useInterpolatedPath(d, ms=600)` — SVG path의 `d` 보간. 명령 구조(M/L/C 순서·개수)가
  같으면 `d3-interpolate` 문자열 보간을 바로 쓰고, 버킷 수가 달라 구조가 어긋나면
  두 path를 64개 점으로 리샘플(`getPointAtLength`)한 뒤 점 단위로 보간해 폴리라인을
  다시 그린다.

## 미해결

- 팔레트가 실질 4색뿐이라 시리즈가 5개 이상이면 `color-mix`로 우려낸 변형색을
  쓴다. 토큰이 늘어나면(디자인 쪽 결정) 교체 대상.
- `Sankey`·`AreaChart`는 각 링크/밴드마다 `useInterpolatedPath` 훅을 개별
  서브컴포넌트(`SankeyLink`/`AreaBand`)에서 호출한다. 링크·시리즈 개수가 아주
  많아지면(예: 서버 수십 대) 훅 인스턴스 수도 그만큼 늘어난다 — 지금 규모(서버
  수십 이내)에서는 문제없다고 보고 넘겼다.
