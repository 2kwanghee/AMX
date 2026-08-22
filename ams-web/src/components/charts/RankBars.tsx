'use client';

import { useLayoutEffect, useRef } from 'react';
import { flipPositions, formatCompact } from './math';
import { prefersReducedMotion } from './motion';

export interface RankBarRow {
  key: string;
  label: string;
  value: number;
}

export interface RankBarsProps {
  /** 이미 내림차순 정렬된 행. 정렬은 호출부(기간·집계 API) 책임이다. */
  rows: RankBarRow[];
  unit?: string;
  formatValue?: (n: number) => string;
  rowHeight?: number;
}

/** 가로 순위 바. 기간이 바뀌어 rows 순서가 재정렬되면 FLIP(First-Last-Invert-Play)
 *  기법으로 각 행이 이전 위치에서 새 위치까지 미끄러지듯 이동한다. React는 key로
 *  이미 새 순서대로 DOM을 재배치해두므로, 여기서는 "직전 위치만큼 어긋난 채로
 *  시작해 0으로 되돌아가는" transform만 걸어준다. */
export function RankBars({ rows, unit = '', formatValue = formatCompact, rowHeight = 32 }: RankBarsProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const rowElsRef = useRef(new Map<string, HTMLDivElement>());
  const prevOrderRef = useRef<string[]>(rows.map((r) => r.key));

  useLayoutEffect(() => {
    const nextOrder = rows.map((r) => r.key);
    const prevOrder = prevOrderRef.current;
    prevOrderRef.current = nextOrder;
    if (prefersReducedMotion()) return;

    const deltas = flipPositions(prevOrder, nextOrder);
    for (const d of deltas) {
      if (d.deltaIndex === 0) continue;
      const el = rowElsRef.current.get(d.key);
      if (!el) continue;
      const dy = d.deltaIndex * rowHeight;
      el.style.transition = 'none';
      el.style.transform = `translateY(${dy}px)`;
      // 강제 리플로우 후 트랜지션을 켜고 0으로 되돌린다(FLIP의 Invert→Play).
      void el.offsetHeight;
      el.style.transition = `transform var(--m-base) var(--ease-inout)`;
      el.style.transform = '';
    }
  }, [rows, rowHeight]);

  if (rows.length === 0) {
    return (
      <div className="chart-empty" role="img" aria-label="순위 데이터 없음">
        표시할 데이터가 없습니다.
      </div>
    );
  }

  const maxValue = Math.max(...rows.map((r) => r.value), 1);

  return (
    <div ref={containerRef} className="chart-rank" role="list" aria-label="순위">
      {rows.map((r) => {
        const pct = Math.max(0, Math.min(100, (r.value / maxValue) * 100));
        return (
          <div
            key={r.key}
            ref={(el) => {
              if (el) rowElsRef.current.set(r.key, el);
              else rowElsRef.current.delete(r.key);
            }}
            className="chart-rank-row"
            style={{ height: rowHeight }}
            role="listitem"
            aria-label={`${r.label} ${formatValue(r.value)}${unit}`}
          >
            <span className="chart-rank-label">{r.label}</span>
            <span className="chart-rank-track">
              <span className="chart-rank-fill" style={{ width: `${pct}%` }} />
            </span>
            <span className="chart-rank-value">
              {formatValue(r.value)}
              {unit}
            </span>
          </div>
        );
      })}
    </div>
  );
}
