// 차트 프리미티브가 공유하는 순수 계산 함수 모음. DOM에 의존하지 않으므로
// vitest node 환경에서 그대로 테스트한다(컴포넌트 자체는 렌더 테스트가 안 되므로
// 로직은 최대한 여기로 뺀다).

import {
  arc as d3Arc,
  area as d3Area,
  curveLinear,
  line as d3Line,
  pie as d3Pie,
  type PieArcDatum,
} from 'd3-shape';
import { sankey as d3Sankey, sankeyLinkHorizontal } from 'd3-sankey';
import { fmtTokens } from '@/lib/usage-format';

// -- 카운트업 -----------------------------------------------------------------

/** 0~1 진행률을 ease-out 큐빅으로 눌러준다. 범위 밖 값은 양 끝으로 클램프한다. */
export function easeOutCubic(t: number): number {
  const p = t < 0 ? 0 : t > 1 ? 1 : t;
  return 1 - (1 - p) ** 3;
}

/** from→to를 진행률 t(0~1)에서 이징 보간한 값. to가 유한하지 않으면 그대로 to를
 *  반환해 NaN이 화면에 번지지 않게 한다. */
export function countUpValue(from: number, to: number, t: number): number {
  if (!Number.isFinite(to)) return to;
  if (!Number.isFinite(from)) return to;
  return from + (to - from) * easeOutCubic(t);
}

/** from→to 구간을 frames+1개 표본으로 나눈 값 배열. useCountUp이 매 rAF마다
 *  호출하는 countUpValue를 미리 계산해두면 테스트에서 애니메이션 타이머 없이
 *  경계 동작을 확인할 수 있다. frames가 0 이하면 [to] 하나만 돌려준다. */
export function countUpFrames(from: number, to: number, frames: number): number[] {
  if (!Number.isFinite(to)) return [];
  if (!Number.isFinite(from) || frames <= 0) return [to];
  const out: number[] = [];
  for (let i = 0; i <= frames; i++) out.push(countUpValue(from, to, i / frames));
  return out;
}

// -- 눈금 ----------------------------------------------------------------------

function niceNum(range: number, round: boolean): number {
  const exponent = Math.floor(Math.log10(range));
  const fraction = range / 10 ** exponent;
  let niceFraction: number;
  if (round) {
    if (fraction < 1.5) niceFraction = 1;
    else if (fraction < 3) niceFraction = 2;
    else if (fraction < 7) niceFraction = 5;
    else niceFraction = 10;
  } else if (fraction <= 1) niceFraction = 1;
  else if (fraction <= 2) niceFraction = 2;
  else if (fraction <= 5) niceFraction = 5;
  else niceFraction = 10;
  return niceFraction * 10 ** exponent;
}

/** [min,max]를 n개 안팎의 "깔끔한" 눈금으로 나눈다(고전 nice-numbers 알고리즘).
 *  min===max면 값 하나짜리 눈금만 돌려주고, 둘 다 0이어도 [0]으로 안전하게
 *  처리한다(log10(0) 회피). n이 0 이하이거나 값이 유한하지 않으면 빈 배열. */
export function niceTicks(min: number, max: number, n = 5): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max) || n <= 0) return [];
  const lo = Math.min(min, max);
  const hi = Math.max(min, max);
  if (lo === hi) return [lo];
  const span = niceNum(hi - lo, false);
  const step = niceNum(span / (n - 1), true);
  const niceMin = Math.floor(lo / step) * step;
  const niceMax = Math.ceil(hi / step) * step;
  const ticks: number[] = [];
  for (let v = niceMin; v <= niceMax + step * 0.5; v += step) {
    // 부동소수 누적 오차 제거(예: 0.1+0.2 잔여물)
    ticks.push(Math.round(v * 1e9) / 1e9);
  }
  return ticks;
}

/** 1.2K/3.4M처럼 자릿수를 압축한 표기. usage-format.ts의 fmtTokens와 완전히
 *  같은 규칙이라 새로 만들지 않고 그대로 재노출한다. */
export const formatCompact = fmtTokens;

// -- 누적 시리즈 ----------------------------------------------------------------

export interface StackedBand {
  key: string;
  /** 버킷별 [y0, y1](쌓기 시작~끝). y1 - y0가 그 버킷의 원래 값이다. */
  values: [number, number][];
}

/** 여러 시리즈를 같은 x축(버킷) 위에 쌓아 누적 영역 좌표로 바꾼다. 값이 없는
 *  버킷은 0으로 채우고, 음수는 방어적으로 0 클램프한다(집계 데이터는 음수가
 *  없다는 전제이지만 렌더가 깨지는 것보다는 0이 낫다). */
export function stackSeries(series: { key: string; values: number[] }[]): StackedBand[] {
  if (series.length === 0) return [];
  const len = Math.max(...series.map((s) => s.values.length), 0);
  const bands: StackedBand[] = series.map((s) => ({ key: s.key, values: [] }));
  for (let i = 0; i < len; i++) {
    let acc = 0;
    series.forEach((s, si) => {
      const raw = s.values[i];
      const v = Math.max(0, Number.isFinite(raw) ? (raw as number) : 0);
      const y0 = acc;
      const y1 = acc + v;
      bands[si]!.values.push([y0, y1]);
      acc = y1;
    });
  }
  return bands;
}

// -- 도넛 ------------------------------------------------------------------

export interface DonutSegment {
  key: string;
  value: number;
  /** 전체 합 대비 비율(0~1). 합계가 0이면 세그먼트 자체가 나오지 않는다. */
  fraction: number;
  /** stroke-dasharray가 아니라 실제 arc path 문자열(호버 팽창 시 outerRadius만
   *  바꿔 다시 그리기 쉽도록). */
  path: string;
  /** 라벨·툴팁 배치용 중간 각도(라디안, 12시 방향이 0). */
  midAngle: number;
}

/** 값 목록을 도넛 arc 경로로 변환한다. 합계가 0이거나 입력이 비어 있으면 빈
 *  배열(그리지 않음 — 0%짜리 원을 그리는 것보다 "데이터 없음" 처리가 호출부
 *  책임이 되는 편이 낫다). */
export function donutArcs(
  values: { key: string; value: number }[],
  opts: { radius?: number; innerRadiusRatio?: number } = {},
): DonutSegment[] {
  const radius = opts.radius ?? 50;
  const innerRadius = radius * (opts.innerRadiusRatio ?? 0.65);
  const total = values.reduce((sum, v) => sum + Math.max(0, v.value), 0);
  if (total <= 0 || values.length === 0) return [];

  const pieGen = d3Pie<{ key: string; value: number }>()
    .value((d) => Math.max(0, d.value))
    .sort(null);
  const arcGen = d3Arc<PieArcDatum<{ key: string; value: number }>>()
    .innerRadius(innerRadius)
    .outerRadius(radius);

  return pieGen(values).map((a) => ({
    key: a.data.key,
    value: a.data.value,
    fraction: a.data.value / total,
    path: arcGen(a) ?? '',
    midAngle: (a.startAngle + a.endAngle) / 2,
  }));
}

// -- 누적 영역 경로 --------------------------------------------------------------

/** 누적 밴드 하나([y0,y1] 쌍 배열)를 폭 w·높이 h 안의 area path 문자열로 만든다.
 *  maxY는 y축 스케일 기준값(보통 전 시리즈 합계 최댓값)이며 0 이하면 전부
 *  바닥선(h)으로 눌러 그린다. curveLinear를 써서 보간(useInterpolatedPath)이
 *  구조가 어긋나지 않게 한다 — 곡선(curveMonotoneX)은 리샘플 없이 직접
 *  보간하기 까다롭다. */
export function areaPath(values: [number, number][], w: number, h: number, maxY: number): string {
  if (values.length === 0) return '';
  const stepX = values.length > 1 ? w / (values.length - 1) : 0;
  const scaleY = (y: number) => (maxY > 0 ? h - (y / maxY) * h : h);
  const gen = d3Area<[number, number]>()
    .x((_d, i) => i * stepX)
    .y0((d) => scaleY(d[0]))
    .y1((d) => scaleY(d[1]))
    .curve(curveLinear);
  return gen(values) ?? '';
}

/** 누적 밴드의 윗변(y1)만 따라가는 선 경로. areaPath와 좌표계·스케일이 같아서
 *  같은 값으로 두 번 불러 면 위에 실루엣 선을 겹쳐 얹을 수 있다. 면을 옅게
 *  깔고 경계만 선으로 세우는 표현에 쓴다. */
export function topLinePath(values: [number, number][], w: number, h: number, maxY: number): string {
  if (values.length === 0) return '';
  const stepX = values.length > 1 ? w / (values.length - 1) : 0;
  const scaleY = (y: number) => (maxY > 0 ? h - (y / maxY) * h : h);
  const gen = d3Line<[number, number]>()
    .x((_d, i) => i * stepX)
    .y((d) => scaleY(d[1]))
    .curve(curveLinear);
  return gen(values) ?? '';
}

// -- 상키(Sankey) ----------------------------------------------------------------

export interface SankeyLayoutNode {
  id: string;
  label: string;
  kind: string;
  x0: number;
  x1: number;
  y0: number;
  y1: number;
}

export interface SankeyLayoutLink {
  source: string;
  target: string;
  value: number;
  width: number;
  path: string;
  /** 링크 path의 시작·끝 좌표(= source 노드 오른쪽 변, target 노드 왼쪽 변의
   *  y0~y1 구간). Sankey.tsx가 "링크가 소스에서 자라나는" 진입 애니메이션의
   *  시작 path를 만들 때 문자열 파싱 없이 바로 쓴다. */
  x0: number;
  x1: number;
  y0: number;
  y1: number;
}

export interface SankeyLayoutResult {
  nodes: SankeyLayoutNode[];
  links: SankeyLayoutLink[];
}

interface SankeyRawNode {
  id: string;
  label: string;
  kind: string;
  index?: number;
  x0?: number;
  x1?: number;
  y0?: number;
  y1?: number;
}

interface SankeyRawLink {
  source: number | SankeyRawNode;
  target: number | SankeyRawNode;
  value: number;
  width?: number;
  // d3-sankey가 레이아웃 계산 중 채워 넣는 필드(입력 시점엔 없음).
  y0?: number;
  y1?: number;
}

/** 서버→계정 흐름을 d3-sankey로 배치한다. 값이 0 이하이거나 자기 자신을
 *  가리키는 링크, 존재하지 않는 노드를 참조하는 링크는 미리 걸러낸다(d3-sankey는
 *  그런 입력에서 NaN 좌표를 뱉는다). 유효한 링크가 하나도 없으면 빈 결과. */
export function sankeyLayout(
  nodes: { id: string; label: string; kind: string }[],
  links: { source: string; target: string; value: number }[],
  w: number,
  h: number,
  opts: { nodeWidth?: number; nodePadding?: number } = {},
): SankeyLayoutResult {
  if (nodes.length === 0) return { nodes: [], links: [] };
  const idIndex = new Map(nodes.map((n, i) => [n.id, i]));
  const cleanLinks = links.filter(
    (l) => idIndex.has(l.source) && idIndex.has(l.target) && l.source !== l.target && l.value > 0,
  );
  if (cleanLinks.length === 0) return { nodes: [], links: [] };

  const graphNodes: SankeyRawNode[] = nodes.map((n) => ({ ...n }));
  const graphLinks: SankeyRawLink[] = cleanLinks.map((l) => ({
    source: idIndex.get(l.source)!,
    target: idIndex.get(l.target)!,
    value: l.value,
  }));

  const width = Math.max(w, 40);
  const height = Math.max(h, 40);
  const layout = d3Sankey<SankeyRawNode, SankeyRawLink>()
    .nodeWidth(opts.nodeWidth ?? 14)
    .nodePadding(opts.nodePadding ?? 12)
    .extent([
      [1, 1],
      [width - 1, height - 1],
    ]);
  const graph = layout({ nodes: graphNodes, links: graphLinks });
  const pathGen = sankeyLinkHorizontal<SankeyRawNode, SankeyRawLink>();

  const outNodes: SankeyLayoutNode[] = graph.nodes
    .filter((n) => n.x0 != null && n.x1 != null && n.y0 != null && n.y1 != null)
    .map((n) => ({ id: n.id, label: n.label, kind: n.kind, x0: n.x0!, x1: n.x1!, y0: n.y0!, y1: n.y1! }));

  const outLinks: SankeyLayoutLink[] = graph.links.map((l) => {
    const source = l.source as SankeyRawNode;
    const target = l.target as SankeyRawNode;
    return {
      source: source.id,
      target: target.id,
      value: l.value,
      width: l.width ?? 0,
      path: pathGen(l) ?? '',
      x0: source.x1 ?? 0,
      x1: target.x0 ?? 0,
      y0: l.y0 ?? 0,
      y1: l.y1 ?? 0,
    };
  });

  return { nodes: outNodes, links: outLinks };
}

// -- 히트맵 색 스케일 --------------------------------------------------------------

/** 히트맵 셀 값(0~max)을 0~1 강도로 정규화하는 함수를 만든다. max가 0 이하거나
 *  유한하지 않으면 항상 0을 돌려주는 함수(빈 히트맵에서 색이 전부 꺼진 상태). */
export function heatmapScale(max: number): (value: number) => number {
  if (!Number.isFinite(max) || max <= 0) return () => 0;
  return (value: number) => {
    if (!Number.isFinite(value) || value <= 0) return 0;
    return Math.max(0, Math.min(1, value / max));
  };
}

// -- FLIP 순위 이동 --------------------------------------------------------------

export interface FlipDelta {
  key: string;
  /** 이전 순번(0부터). 새로 등장한 항목은 -1. */
  prevIndex: number;
  /** 새 순번(0부터). */
  nextIndex: number;
  /** prevIndex - nextIndex. 양수면 위로, 음수면 아래로 이동. 새 항목은 0(진입
   *  애니메이션은 별도 처리). */
  deltaIndex: number;
}

/** 순위 바가 재정렬될 때 각 항목이 몇 칸 이동했는지 계산한다(FLIP: First, Last,
 *  Invert, Play). 실제 픽셀 이동값은 호출부가 deltaIndex * rowHeight로 구한다. */
export function flipPositions(prevOrder: string[], nextOrder: string[]): FlipDelta[] {
  const prevIdx = new Map(prevOrder.map((k, i) => [k, i]));
  return nextOrder.map((key, nextIndex) => {
    const prevIndex = prevIdx.has(key) ? prevIdx.get(key)! : -1;
    return { key, prevIndex, nextIndex, deltaIndex: prevIndex === -1 ? 0 : prevIndex - nextIndex };
  });
}
