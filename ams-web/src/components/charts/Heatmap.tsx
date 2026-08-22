'use client';

import type { CSSProperties } from 'react';
import { heatmapScale } from './math';

// --intensity 커스텀 프로퍼티는 표준 CSSProperties 타입에 없다. React 관례대로
// 객체를 만든 뒤 CSSProperties로 캐스트해 전달한다.
function cellStyle(intensity: number, delayMs: number): CSSProperties {
  return { '--intensity': intensity, animationDelay: `${delayMs}ms` } as CSSProperties;
}

export interface HeatmapProps {
  /** cells[요일][시간] = 세션 수. 요일 인덱스 0=월요일(ISO), 시간은 0~23(UTC). */
  cells: number[][];
  weekdayLabels?: string[];
}

const DEFAULT_WEEKDAYS = ['월', '화', '수', '목', '금', '토', '일'];
const STAGGER_STEP_MS = 30;
const STAGGER_CAP_MS = 300;

/** 요일×시간 세션 히트맵. CSS grid로 배치하고 진입 시 셀마다 index*30ms(최대
 *  300ms) staggered fade-in을 건다. reduced-motion이면 CSS에서 애니메이션 자체를
 *  꺼서(전역 규칙) 이 컴포넌트는 delay 계산만 하고 신경 쓰지 않아도 된다. */
export function Heatmap({ cells, weekdayLabels = DEFAULT_WEEKDAYS }: HeatmapProps) {
  const max = cells.reduce((m, row) => Math.max(m, ...row), 0);
  const scale = heatmapScale(max);

  if (cells.length === 0 || cells.every((row) => row.length === 0)) {
    return (
      <div className="chart-empty" role="img" aria-label="세션 히트맵 데이터 없음">
        표시할 데이터가 없습니다.
      </div>
    );
  }

  return (
    <div className="chart-heatmap" role="img" aria-label="요일별 시간대 세션 히트맵">
      <div className="chart-heatmap-hours" aria-hidden="true">
        {Array.from({ length: 24 }, (_, h) => (
          <span key={h} className="chart-heatmap-hour-label">
            {h % 6 === 0 ? h : ''}
          </span>
        ))}
      </div>
      {cells.map((row, day) => (
        <div key={day} className="chart-heatmap-row">
          <span className="chart-heatmap-day-label">{weekdayLabels[day] ?? ''}</span>
          <div className="chart-heatmap-cells">
            {row.map((value, hour) => {
              const idx = day * 24 + hour;
              const intensity = scale(value);
              const delay = Math.min(idx * STAGGER_STEP_MS, STAGGER_CAP_MS);
              return (
                <span
                  key={hour}
                  className="chart-heatmap-cell"
                  // 진하기는 배경색(--intensity)으로, 진입 애니메이션은 opacity/transform으로
                  // 서로 다른 속성을 써서 겹치지 않게 한다(둘 다 opacity를 건드리면 애니메이션이
                  // 끝난 뒤 인라인 값으로 되돌아가며 깜빡이는 문제가 생긴다).
                  style={cellStyle(intensity, delay)}
                  title={`${weekdayLabels[day] ?? ''}요일 ${hour}시: ${value}건`}
                />
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}
