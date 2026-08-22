'use client';

import { useId, useMemo, useState } from 'react';
import { areaPath, formatCompact, niceTicks, stackSeries, type StackedBand } from './math';
import { useInterpolatedPath } from './useInterpolatedPath';
import { OTHER_COLOR, seriesColor } from './palette';

export interface AreaChartSeriesInput {
  key: string;
  label: string;
  values: number[];
}

export interface AreaChartProps {
  /** ISO 문자열 버킷 경계. series[].values와 길이가 같아야 한다. */
  buckets: string[];
  /** 상위 8개 + other로 이미 잘려서 오는 것을 기대한다(stats/timeseries 계약). */
  series: AreaChartSeriesInput[];
  unit: 'tokens' | 'seconds';
  width?: number;
  height?: number;
}

function formatBucketLabel(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('ko-KR', { month: 'numeric', day: 'numeric', hour: '2-digit', hour12: false });
}

function unitSuffix(unit: 'tokens' | 'seconds'): string {
  return unit === 'seconds' ? '초' : '토큰';
}

// 밴드 하나. 자기 자신의 path 보간 훅을 가져야 하므로 map 안에서 훅을 쓰지 않고
// 이 서브컴포넌트로 분리한다(리스트 길이가 변해도 각 밴드는 key로 안정적으로
// 마운트/언마운트된다). mountFrom(바닥선 path)을 넘겨 마운트 때도 실제로
// 채워 올라오는 진입 애니메이션이 재생되게 한다.
function AreaBand({ d, color, mountFrom }: { d: string; color: string; mountFrom: string }) {
  const animatedD = useInterpolatedPath(d, 500, mountFrom);
  return <path d={animatedD} fill={color} className="chart-area-band" />;
}

/** 누적 영역 시계열. 마운트 시 0에서 드로우하듯 보이도록 첫 프레임은 바닥선에서
 *  시작해 useInterpolatedPath가 자연히 채워 올린다. 호버하면 크로스헤어와
 *  버킷별 시리즈 값 툴팁을 보여준다. */
export function AreaChart({ buckets, series, unit, width = 640, height = 220 }: AreaChartProps) {
  const gid = useId();
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  const padLeft = 40;
  const padBottom = 22;
  const padTop = 8;
  const innerW = Math.max(1, width - padLeft - 8);
  const innerH = Math.max(1, height - padTop - padBottom);

  const bands = useMemo(() => stackSeries(series), [series]);
  const maxY = useMemo(() => {
    let m = 0;
    for (const band of bands) {
      for (const [, y1] of band.values) if (y1 > m) m = y1;
    }
    return m;
  }, [bands]);
  const ticks = useMemo(() => niceTicks(0, maxY, 4), [maxY]);
  const tickMax = ticks.length > 0 ? ticks[ticks.length - 1]! : maxY;

  // 바닥선(모든 버킷이 0) 상태의 path. areaPath로 실제 값과 같은 구조(같은
  // 점 개수)로 만들어야 useInterpolatedPath가 리샘플 없이 바로 보간한다.
  const bandsWithColor: { key: string; color: string; d: string; mountFrom: string; band: StackedBand }[] =
    bands.map((band, i) => {
      const zeroed: [number, number][] = band.values.map(() => [0, 0]);
      return {
        key: band.key,
        color: band.key === 'other' ? OTHER_COLOR : seriesColor(i),
        d: areaPath(band.values, innerW, innerH, tickMax || 1),
        mountFrom: areaPath(zeroed, innerW, innerH, tickMax || 1),
        band,
      };
    });

  const bucketCount = buckets.length;
  const stepX = bucketCount > 1 ? innerW / (bucketCount - 1) : 0;

  if (bucketCount === 0 || series.length === 0) {
    return (
      <div className="chart-empty" role="img" aria-label={`${unitSuffix(unit)} 시계열 데이터 없음`}>
        표시할 데이터가 없습니다.
      </div>
    );
  }

  const altText = `${unitSuffix(unit)} 시계열, ${series.map((s) => s.label).join(', ')} ${bucketCount}개 구간`;

  return (
    <div className="chart-area-wrap">
      <svg
        className="chart-area-svg"
        viewBox={`0 0 ${width} ${height}`}
        width={width}
        height={height}
        role="img"
        aria-label={altText}
        onMouseLeave={() => setHoverIdx(null)}
        onMouseMove={(e) => {
          const rect = e.currentTarget.getBoundingClientRect();
          const scaleX = width / rect.width;
          const x = (e.clientX - rect.left) * scaleX - padLeft;
          const idx = Math.round(x / (stepX || 1));
          setHoverIdx(Math.max(0, Math.min(bucketCount - 1, idx)));
        }}
      >
        <g transform={`translate(${padLeft},${padTop})`}>
          {ticks.map((t) => {
            const y = tickMax > 0 ? innerH - (t / tickMax) * innerH : innerH;
            return (
              <g key={t}>
                <line x1={0} x2={innerW} y1={y} y2={y} className="chart-grid-line" />
                <text x={-8} y={y} className="chart-axis-label" textAnchor="end" dominantBaseline="middle">
                  {formatCompact(t)}
                </text>
              </g>
            );
          })}
          {bandsWithColor.map((b) => (
            <AreaBand key={b.key} d={b.d} color={b.color} mountFrom={b.mountFrom} />
          ))}
          {hoverIdx != null && (
            <line
              x1={hoverIdx * stepX}
              x2={hoverIdx * stepX}
              y1={0}
              y2={innerH}
              className="chart-crosshair"
              aria-hidden="true"
            />
          )}
          <text x={0} y={innerH + 16} className="chart-axis-label" textAnchor="start">
            {buckets[0] ? formatBucketLabel(buckets[0]) : ''}
          </text>
          <text x={innerW} y={innerH + 16} className="chart-axis-label" textAnchor="end">
            {buckets[bucketCount - 1] ? formatBucketLabel(buckets[bucketCount - 1]!) : ''}
          </text>
        </g>
      </svg>
      {hoverIdx != null && (
        <div
          className="chart-tooltip"
          style={{ left: `${padLeft + hoverIdx * stepX}px`, top: `${padTop}px` }}
        >
          <div className="chart-tooltip-head">{buckets[hoverIdx] ? formatBucketLabel(buckets[hoverIdx]!) : ''}</div>
          {bandsWithColor.map((b) => {
            const raw = series.find((s) => s.key === b.key)?.values[hoverIdx];
            return (
              <div key={`${gid}-${b.key}`} className="chart-tooltip-row">
                <span className="chart-tooltip-swatch" style={{ background: b.color }} />
                <span className="chart-tooltip-label">{series.find((s) => s.key === b.key)?.label ?? b.key}</span>
                <span className="chart-tooltip-value">
                  {formatCompact(raw ?? 0)} {unitSuffix(unit)}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
