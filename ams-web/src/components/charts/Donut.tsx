'use client';

import { useState } from 'react';
import { donutArcs, formatCompact } from './math';
import { OTHER_COLOR, seriesColor } from './palette';

export interface DonutValue {
  key: string;
  label: string;
  value: number;
}

export interface DonutProps {
  values: DonutValue[];
  size?: number;
  unit?: string;
  /** 중앙에 보여줄 합계 라벨. 기본은 "합계". */
  totalLabel?: string;
}

/** 도넛(링) 차트. stroke가 아니라 arc path로 그려서 세그먼트별 호버 팽창이
 *  자연스럽다. 중앙에는 기본으로 합계를, 세그먼트를 호버하면 그 세그먼트의
 *  값·비율로 바뀐다. 범례는 아래 목록으로 별도 표시(색 판별이 어려운 사용자를
 *  위한 텍스트 대체이기도 하다). */
export function Donut({ values, size = 140, unit = '', totalLabel = '합계' }: DonutProps) {
  const [hoverKey, setHoverKey] = useState<string | null>(null);
  const radius = size / 2 - 4;
  const segments = donutArcs(
    values.map((v) => ({ key: v.key, value: v.value })),
    { radius, innerRadiusRatio: 0.62 },
  );
  const total = values.reduce((sum, v) => sum + Math.max(0, v.value), 0);

  if (segments.length === 0 || total <= 0) {
    return (
      <div className="chart-empty" role="img" aria-label="분포 데이터 없음">
        표시할 데이터가 없습니다.
      </div>
    );
  }

  const hovered = hoverKey != null ? values.find((v) => v.key === hoverKey) : undefined;
  const centerBig = hovered ? formatCompact(hovered.value) : formatCompact(total);
  const centerSmall = hovered
    ? `${hovered.label} ${Math.round((hovered.value / total) * 100)}%`
    : `${totalLabel}${unit ? ` (${unit})` : ''}`;

  const altText = values
    .map((v) => `${v.label} ${Math.round((v.value / total) * 100)}%`)
    .join(', ');

  return (
    <div className="chart-donut-wrap">
      <svg
        className="chart-donut-svg"
        viewBox={`${-size / 2} ${-size / 2} ${size} ${size}`}
        width={size}
        height={size}
        role="img"
        aria-label={`${totalLabel} 분포: ${altText}`}
      >
        {segments.map((seg, i) => (
          <path
            key={seg.key}
            d={seg.path}
            fill={seg.key === 'other' ? OTHER_COLOR : seriesColor(i)}
            className={`chart-donut-seg ${hoverKey === seg.key ? 'hover' : ''}`}
            onMouseEnter={() => setHoverKey(seg.key)}
            onMouseLeave={() => setHoverKey((k) => (k === seg.key ? null : k))}
          />
        ))}
        <text className="chart-donut-big" x={0} y={-3} textAnchor="middle">
          {centerBig}
        </text>
        <text className="chart-donut-small" x={0} y={13} textAnchor="middle">
          {centerSmall}
        </text>
      </svg>
      <ul className="chart-legend">
        {values.map((v, i) => (
          <li
            key={v.key}
            className={`chart-legend-item ${hoverKey === v.key ? 'hover' : ''}`}
            onMouseEnter={() => setHoverKey(v.key)}
            onMouseLeave={() => setHoverKey((k) => (k === v.key ? null : k))}
          >
            <span
              className="chart-legend-swatch"
              style={{ background: v.key === 'other' ? OTHER_COLOR : seriesColor(i) }}
            />
            <span className="chart-legend-label">{v.label}</span>
            <span className="chart-legend-value">{Math.round((v.value / total) * 100)}%</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
