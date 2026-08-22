'use client';

import { useMemo, useState } from 'react';
import { formatCompact, sankeyLayout } from './math';
import { useInterpolatedPath } from './useInterpolatedPath';

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
  width?: number;
  height?: number;
}

// 링크 하나. grow 진입 애니메이션(폭 보간)을 훅으로 걸어야 해서 map 밖 컴포넌트로
// 분리한다(AreaBand와 같은 이유).
function SankeyLink({
  d,
  mountFrom,
  strokeWidth,
  highlighted,
  dimmed,
  onEnter,
  onLeave,
}: {
  d: string;
  mountFrom: string;
  strokeWidth: number;
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
      style={{ strokeWidth }}
      onMouseEnter={onEnter}
      onMouseLeave={onLeave}
    />
  );
}

/** 서버→계정 사용량 흐름 상키 다이어그램. 링크는 값에 비례한 굵기로 그리고,
 *  호버하면 그 링크만 강조하고 나머지는 흐리게 한다(경로 하이라이트). */
export function Sankey({ nodes, links, unit = '', width = 640, height = 320 }: SankeyProps) {
  const [hoverLink, setHoverLink] = useState<number | null>(null);
  const layout = useMemo(() => sankeyLayout(nodes, links, width, height), [nodes, links, width, height]);

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
    <svg
      className="chart-sankey-svg"
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      role="img"
      aria-label={`서버-계정 사용량 흐름: ${altText}`}
    >
      <g className="chart-sankey-links">
        {layout.links.map((l, i) => (
          <SankeyLink
            key={`${l.source}-${l.target}-${i}`}
            d={l.path}
            // 소스 쪽 점(x0)에 y0~y1을 그대로 눌러붙인 뒤 x0로만 그린 시작
            // path. pathGen이 뱉는 "M x,y C cx,y cx,y x,y" 구조(M 2개 + C 6개
            // 숫자)와 명령 개수·순서가 같아 리샘플 없이 바로 보간된다.
            mountFrom={`M${l.x0},${l.y0}C${l.x0},${l.y0},${l.x0},${l.y1},${l.x0},${l.y1}`}
            strokeWidth={Math.max(1, l.width)}
            highlighted={hoverLink === i}
            dimmed={hoverLink != null && hoverLink !== i}
            onEnter={() => setHoverLink(i)}
            onLeave={() => setHoverLink((v) => (v === i ? null : v))}
          />
        ))}
      </g>
      <g className="chart-sankey-nodes">
        {layout.nodes.map((n) => (
          <g key={n.id} transform={`translate(${n.x0},${n.y0})`}>
            <rect
              className={`chart-sankey-node ${n.kind}`}
              width={Math.max(1, n.x1 - n.x0)}
              height={Math.max(1, n.y1 - n.y0)}
            />
            <text
              className="chart-sankey-node-label"
              x={n.kind === 'server' ? -6 : n.x1 - n.x0 + 6}
              y={(n.y1 - n.y0) / 2}
              textAnchor={n.kind === 'server' ? 'end' : 'start'}
              dominantBaseline="middle"
            >
              {n.label}
            </text>
          </g>
        ))}
      </g>
    </svg>
  );
}
