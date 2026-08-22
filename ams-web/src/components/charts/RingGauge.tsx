'use client';

import { useCountUp } from './useCountUp';

export interface RingGaugeProps {
  /** 사용률(%). 0~100으로 클램프한다. 게이지 채움·색은 이 값 기준(ServerNode의
   *  gaugeTone과 같은 70/90 임계) — "잔여"가 아니라 "사용률"을 그린다. */
  pct: number;
  /** 원 아래 캡션. 지정하지 않으면 "잔여 N%"를 자동 계산해 보여준다(계정
   *  잔여 사용량 결정: 게이지는 사용률 유지, 텍스트만 잔여 병기). */
  remainingText?: string;
  /** 원 중앙 위 작은 라벨(예: "5시간", "7일"). */
  windowLabel?: string;
  size?: number;
  strokeWidth?: number;
}

// ServerNode.tsx의 gaugeTone과 같은 90/70 임계 규칙(중복이지만 export되어 있지
// 않아 그대로 복제했다 — 공용 유틸로 뺄지는 이번 PR 범위 밖).
function gaugeTone(pct: number): '' | 'warn' | 'crit' {
  if (pct >= 90) return 'crit';
  if (pct >= 70) return 'warn';
  return '';
}

/** 원형 잔여 게이지. 채움은 사용률(pct)에 비례해 그리되, 문구는 "잔여"로
 *  뒤집어 보여준다. pct가 없거나 유효하지 않으면 "미보고" 상태로 폴백한다. */
export function RingGauge({ pct, remainingText, windowLabel, size = 88, strokeWidth = 8 }: RingGaugeProps) {
  const missing = pct == null || Number.isNaN(pct);
  const clamped = missing ? 0 : Math.max(0, Math.min(100, pct));
  const animatedPct = useCountUp(clamped, 500);
  const tone = gaugeTone(clamped);

  const radius = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference * (1 - animatedPct / 100);
  const remaining = Math.round(100 - clamped);
  const caption = remainingText ?? (missing ? '메트릭 미보고' : `잔여 ${remaining}%`);

  return (
    <div className="chart-ring" role="img" aria-label={`${windowLabel ? `${windowLabel} ` : ''}${caption}`}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
        <circle
          className="chart-ring-track"
          cx={size / 2}
          cy={size / 2}
          r={radius}
          strokeWidth={strokeWidth}
          fill="none"
        />
        {!missing && (
          <circle
            className={`chart-ring-fill ${tone}`}
            cx={size / 2}
            cy={size / 2}
            r={radius}
            strokeWidth={strokeWidth}
            fill="none"
            strokeDasharray={circumference}
            strokeDashoffset={offset}
            strokeLinecap="round"
            transform={`rotate(-90 ${size / 2} ${size / 2})`}
          />
        )}
        <text x={size / 2} y={size / 2 + 5} textAnchor="middle" className="chart-ring-text">
          {missing ? '—' : `${remaining}%`}
        </text>
      </svg>
      {windowLabel && <div className="chart-ring-window">{windowLabel}</div>}
      <div className="chart-ring-caption">{caption}</div>
    </div>
  );
}
