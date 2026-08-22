'use client';

import { useMemo, useState } from 'react';
import { formatCompact, sankeyLayout } from './math';
import { useInterpolatedPath } from './useInterpolatedPath';
import { useMeasuredWidth } from './useMeasuredWidth';
import { seriesColor } from './palette';

export interface SankeyNodeInput {
  id: string;
  label: string;
  kind: 'server' | 'account';
}

export interface SankeyLinkInput {
  source: string;
  target: string;
  value: number;
}

export interface SankeyProps {
  nodes: SankeyNodeInput[];
  links: SankeyLinkInput[];
  unit?: string;
  /** 다이어그램 높이(px). 폭은 컨테이너를 실측해 1:1로 맞춘다. */
  height?: number;
}

const NODE_WIDTH = 10;
const LABEL_PAD = 88; // 좌우 라벨이 앉을 여백
const MAX_LABEL = 16;
const MAX_BAND = 44; // 가장 두꺼운 링크가 가질 최대 두께
const MIN_CONTENT_H = 104; // 노드 라벨이 겹치지 않을 최소 높이

/** 계정은 이메일 로컬파트만, 서버는 이름 그대로. 둘 다 길면 말줄임한다. */
function shortLabel(label: string, kind: 'server' | 'account'): string {
  const base = kind === 'account' ? (label.split('@')[0] ?? label) : label;
  return base.length > MAX_LABEL ? `${base.slice(0, MAX_LABEL - 1)}…` : base;
}

// 링크 하나. grow 진입 애니메이션(폭 보간)을 훅으로 걸어야 해서 map 밖 컴포넌트로
// 분리한다(AreaBand와 같은 이유).
function SankeyLink({
  d,
  mountFrom,
  strokeWidth,
  color,
  highlighted,
  dimmed,
  onEnter,
  onLeave,
}: {
  d: string;
  mountFrom: string;
  strokeWidth: number;
  color: string;
  highlighted: boolean;
  dimmed: boolean;
  onEnter: () => void;
  onLeave: () => void;
}) {
  const animatedD = useInterpolatedPath(d, 500, mountFrom);
  return (
    <path
      d={animatedD}
      className={`chart-sankey-link ${highlighted ? 'hover' : ''} ${dimmed ? 'dim' : ''}`}
      style={{ strokeWidth, stroke: color }}
      onMouseEnter={onEnter}
      onMouseLeave={onLeave}
    />
  );
}

/** 서버→계정 사용량 흐름 상키 다이어그램. 링크는 값에 비례한 굵기로 그리고,
 *  호버하면 그 링크만 강조하고 나머지는 흐리게 한다(경로 하이라이트). 색은
 *  소스 서버별로 돌려서 링크 하나짜리 흐름도 어느 서버에서 나왔는지 읽힌다. */
export function Sankey({ nodes, links, unit = '', height = 200 }: SankeyProps) {
  const [hoverLink, setHoverLink] = useState<number | null>(null);
  const [wrapRef, width] = useMeasuredWidth<HTMLDivElement>(400);

  // 라벨은 노드 바깥에 붙으므로 그 폭만큼 안쪽으로 밀어 배치한다. 그러지 않으면
  // 양 끝 라벨이 카드 밖으로 잘린다.
  const innerW = Math.max(60, width - LABEL_PAD * 2);
  // d3-sankey는 가장 두꺼운 열이 extent 높이를 꽉 채우도록 스케일을 잡는다.
  // 그래서 한 링크가 사용량의 대부분을 차지하면 그 하나가 카드 전체를 덮는
  // 블록이 되어버린다. 한 번 배치해 보고 가장 두꺼운 링크가 MAX_BAND를 넘으면
  // 그 비율만큼 줄인 높이로 다시 배치한 뒤 남는 자리를 위아래로 나눈다 —
  // 스케일이 전체에 같이 걸리므로 링크끼리의 굵기 비율은 그대로다.
  const layout = useMemo(() => {
    // nodePadding은 라벨 한 줄(11px)이 서로 겹치지 않을 만큼 벌린다. 값이
    // 아주 작은 노드끼리는 이 여백이 유일한 간격이다.
    const opts = { nodeWidth: NODE_WIDTH, nodePadding: 14 };
    const first = sankeyLayout(nodes, links, innerW, height, opts);
    const maxW = first.links.reduce((m, l) => Math.max(m, l.width), 0);
    if (maxW <= MAX_BAND) return first;
    const shrunk = Math.max(MIN_CONTENT_H, Math.round(height * (MAX_BAND / maxW)));
    if (shrunk >= height) return first;
    return sankeyLayout(nodes, links, innerW, shrunk, opts);
  }, [nodes, links, innerW, height]);

  // 재배치된 결과의 실제 높이만큼만 쓰고 나머지는 여백으로 남긴다.
  const contentH = layout.nodes.reduce((m, n) => Math.max(m, n.y1), 0);
  const offsetY = Math.max(0, (height - contentH) / 2);

  // 서버(소스) 순서대로 색을 배정한다. 링크는 자기 소스의 색을 물려받는다.
  const colorOfNode = useMemo(() => {
    const map = new Map<string, string>();
    let i = 0;
    for (const n of nodes) {
      if (n.kind !== 'server') continue;
      map.set(n.id, seriesColor(i));
      i += 1;
    }
    return map;
  }, [nodes]);

  if (layout.nodes.length === 0 || layout.links.length === 0) {
    return (
      <div className="chart-empty" role="img" aria-label="서버-계정 흐름 데이터 없음">
        표시할 데이터가 없습니다.
      </div>
    );
  }

  const altText = layout.links
    .map((l) => {
      const s = layout.nodes.find((n) => n.id === l.source)?.label ?? l.source;
      const t = layout.nodes.find((n) => n.id === l.target)?.label ?? l.target;
      return `${s} → ${t} ${formatCompact(l.value)}${unit}`;
    })
    .join(', ');

  return (
    <div className="chart-sankey-wrap" ref={wrapRef}>
      <svg
        className="chart-sankey-svg"
        viewBox={`0 0 ${width} ${height}`}
        width={width}
        height={height}
        role="img"
        aria-label={`서버-계정 사용량 흐름: ${altText}`}
      >
        <g transform={`translate(${LABEL_PAD},${offsetY})`}>
          <g className="chart-sankey-links">
            {layout.links.map((l, i) => (
              <SankeyLink
                key={`${l.source}-${l.target}-${i}`}
                d={l.path}
                // 소스 쪽 점(x0)에 y0~y1을 그대로 눌러붙인 뒤 x0로만 그린 시작
                // path. pathGen이 뱉는 "M x,y C cx,y cx,y x,y" 구조(M 2개 + C 6개
                // 숫자)와 명령 개수·순서가 같아 리샘플 없이 바로 보간된다.
                mountFrom={`M${l.x0},${l.y0}C${l.x0},${l.y0},${l.x0},${l.y1},${l.x0},${l.y1}`}
                strokeWidth={Math.max(1.5, l.width)}
                color={colorOfNode.get(l.source) ?? 'var(--accent)'}
                highlighted={hoverLink === i}
                dimmed={hoverLink != null && hoverLink !== i}
                onEnter={() => setHoverLink(i)}
                onLeave={() => setHoverLink((v) => (v === i ? null : v))}
              />
            ))}
          </g>
          <g className="chart-sankey-nodes">
            {layout.nodes.map((n) => {
              const kind = n.kind === 'account' ? 'account' : 'server';
              return (
                <g key={n.id} transform={`translate(${n.x0},${n.y0})`}>
                  <rect
                    className={`chart-sankey-node ${kind}`}
                    width={Math.max(1, n.x1 - n.x0)}
                    height={Math.max(2, n.y1 - n.y0)}
                    rx={2}
                    style={kind === 'server' ? { fill: colorOfNode.get(n.id) ?? 'var(--accent)' } : undefined}
                  />
                  <text
                    className="chart-sankey-node-label"
                    x={kind === 'server' ? -8 : n.x1 - n.x0 + 8}
                    y={Math.max(2, n.y1 - n.y0) / 2}
                    textAnchor={kind === 'server' ? 'end' : 'start'}
                    dominantBaseline="middle"
                  >
                    {shortLabel(n.label, kind)}
                  </text>
                </g>
              );
            })}
          </g>
        </g>
      </svg>
    </div>
  );
}
