'use client';

import { useId, useMemo, useState } from 'react';
import { areaPath, formatCompact, niceTicks, stackSeries, topLinePath, type StackedBand } from './math';
import { useInterpolatedPath } from './useInterpolatedPath';
import { useMeasuredWidth } from './useMeasuredWidth';
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
  /** 플롯 영역 높이(px). 폭은 컨테이너를 실측해 1:1로 맞춘다. */
  height?: number;
}

/** 버킷 간격이 하루 미만이면 시각까지, 그 이상이면 날짜만 보여준다. 30일 구간의
 *  x축에 "8. 15. 00시"가 여섯 번 늘어서면 읽는 데 방해만 된다. */
function makeBucketLabeler(buckets: string[]): (iso: string) => string {
  const t0 = buckets.length > 1 ? new Date(buckets[0]!).getTime() : NaN;
  const t1 = buckets.length > 1 ? new Date(buckets[1]!).getTime() : NaN;
  const stepMs = Number.isFinite(t0) && Number.isFinite(t1) ? Math.abs(t1 - t0) : NaN;
  const withHour = Number.isFinite(stepMs) && stepMs < 24 * 3600 * 1000;
  return (iso: string) => {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    const md = `${d.getMonth() + 1}/${d.getDate()}`;
    return withHour ? `${md} ${d.getHours()}시` : md;
  };
}

/** 축에 실제로 찍을 버킷 인덱스. 양 끝은 반드시 포함하고 그 사이를 균등하게
 *  나눠 최대 6개까지만 남긴다. */
function tickIndices(count: number, max = 6): number[] {
  if (count <= 0) return [];
  if (count <= max) return Array.from({ length: count }, (_, i) => i);
  const step = (count - 1) / (max - 1);
  const out = new Set<number>();
  for (let i = 0; i < max; i++) out.add(Math.round(i * step));
  return [...out].sort((a, b) => a - b);
}

function unitSuffix(unit: 'tokens' | 'seconds'): string {
  return unit === 'seconds' ? '초' : '토큰';
}

// 밴드 하나. 자기 자신의 path 보간 훅을 가져야 하므로 map 안에서 훅을 쓰지 않고
// 이 서브컴포넌트로 분리한다(리스트 길이가 변해도 각 밴드는 key로 안정적으로
// 마운트/언마운트된다). mountFrom(바닥선 path)을 넘겨 마운트 때도 실제로
// 채워 올라오는 진입 애니메이션이 재생되게 한다. 면은 옅게 깔고 윗변만 선으로
// 세워 경계를 또렷하게 만든다 — 불투명 단색으로 채우면 겹친 시리즈가 벽처럼
// 보인다.
function AreaBand({
  d,
  line,
  lineFrom,
  color,
  mountFrom,
}: {
  d: string;
  line: string;
  lineFrom: string;
  color: string;
  mountFrom: string;
}) {
  const animatedD = useInterpolatedPath(d, 500, mountFrom);
  const animatedLine = useInterpolatedPath(line, 500, lineFrom);
  return (
    <>
      <path d={animatedD} fill={color} className="chart-area-band" />
      <path d={animatedLine} stroke={color} className="chart-area-line" />
    </>
  );
}

/** 누적 영역 시계열. 마운트 시 0에서 드로우하듯 보이도록 첫 프레임은 바닥선에서
 *  시작해 useInterpolatedPath가 자연히 채워 올린다. 호버하면 크로스헤어와
 *  버킷별 시리즈 값 툴팁을 보여준다. */
export function AreaChart({ buckets, series, unit, height = 220 }: AreaChartProps) {
  const gid = useId();
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const [wrapRef, width] = useMeasuredWidth<HTMLDivElement>(640);

  const padLeft = 44;
  const padRight = 10;
  const padBottom = 22;
  const padTop = 10;
  const innerW = Math.max(1, width - padLeft - padRight);
  const innerH = Math.max(1, height - padTop - padBottom);

  const bands = useMemo(() => stackSeries(series), [series]);
  const maxY = useMemo(() => {
    let m = 0;
    for (const band of bands) {
      for (const [, y1] of band.values) if (y1 > m) m = y1;
    }
    return m;
  }, [bands]);
  // 5를 넘기면 눈금 간격이 잘게 쪼개져 4~5개(0 포함)로 떨어진다. 3개짜리
  // 축은 중간값을 눈으로 짚을 수 없다.
  const ticks = useMemo(() => niceTicks(0, maxY, 5), [maxY]);
  const tickMax = ticks.length > 0 ? ticks[ticks.length - 1]! : maxY;

  // 바닥선(모든 버킷이 0) 상태의 path. areaPath로 실제 값과 같은 구조(같은
  // 점 개수)로 만들어야 useInterpolatedPath가 리샘플 없이 바로 보간한다.
  const bandsWithColor: {
    key: string;
    color: string;
    d: string;
    mountFrom: string;
    line: string;
    lineFrom: string;
    band: StackedBand;
  }[] = bands.map((band, i) => {
    const zeroed: [number, number][] = band.values.map(() => [0, 0]);
    return {
      key: band.key,
      color: band.key === 'other' ? OTHER_COLOR : seriesColor(i),
      d: areaPath(band.values, innerW, innerH, tickMax || 1),
      mountFrom: areaPath(zeroed, innerW, innerH, tickMax || 1),
      line: topLinePath(band.values, innerW, innerH, tickMax || 1),
      lineFrom: topLinePath(zeroed, innerW, innerH, tickMax || 1),
      band,
    };
  });

  const bucketCount = buckets.length;
  const stepX = bucketCount > 1 ? innerW / (bucketCount - 1) : 0;
  const labelOf = useMemo(() => makeBucketLabeler(buckets), [buckets]);
  const xTicks = useMemo(() => tickIndices(bucketCount), [bucketCount]);

  if (bucketCount === 0 || series.length === 0) {
    return (
      <div className="chart-empty" role="img" aria-label={`${unitSuffix(unit)} 시계열 데이터 없음`}>
        표시할 데이터가 없습니다.
      </div>
    );
  }

  const altText = `${unitSuffix(unit)} 시계열, ${series.map((s) => s.label).join(', ')} ${bucketCount}개 구간`;
  const scaleY = (y: number) => (tickMax > 0 ? innerH - (y / tickMax) * innerH : innerH);

  return (
    <div className="chart-area-wrap" ref={wrapRef}>
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
            const y = scaleY(t);
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
            <AreaBand key={b.key} d={b.d} line={b.line} lineFrom={b.lineFrom} color={b.color} mountFrom={b.mountFrom} />
          ))}
          {bandsWithColor.map((b) => {
            const last = b.band.values[b.band.values.length - 1];
            if (!last) return null;
            return (
              <circle
                key={`dot-${b.key}`}
                className="chart-area-dot"
                cx={innerW}
                cy={scaleY(last[1])}
                r={3}
                fill={b.color}
              />
            );
          })}
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
          {xTicks.map((i) => (
            <text
              key={`x-${i}`}
              x={i * stepX}
              y={innerH + 15}
              className="chart-axis-label"
              textAnchor={i === 0 ? 'start' : i === bucketCount - 1 ? 'end' : 'middle'}
            >
              {buckets[i] ? labelOf(buckets[i]!) : ''}
            </text>
          ))}
        </g>
      </svg>
      {hoverIdx != null && (
        <div
          className="chart-tooltip"
          style={{ left: `${padLeft + hoverIdx * stepX}px`, top: `${padTop}px` }}
        >
          <div className="chart-tooltip-head">{buckets[hoverIdx] ? labelOf(buckets[hoverIdx]!) : ''}</div>
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
