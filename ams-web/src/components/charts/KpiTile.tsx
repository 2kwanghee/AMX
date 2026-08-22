'use client';

import type { ReactNode } from 'react';
import { Sparkline } from '../common';
import { useCountUp } from './useCountUp';
import { formatCompact } from './math';

export interface KpiTileProps {
  label: string;
  value: number;
  /** 직전 구간 값. 있으면 증감 화살표·퍼센트를 같이 보여준다. */
  prevValue?: number;
  /** 값 표기 함수. 기본은 K/M/B 압축(formatCompact). 비용처럼 이미 문자열로
   *  포맷된 값을 쓰려면 이 prop 대신 valueText를 쓴다. */
  formatValue?: (n: number) => string;
  /** value가 숫자가 아니라(비용 문자열 등) 그대로 보여줄 때. 지정하면 카운트업
   *  애니메이션은 건너뛰고 즉시 이 텍스트를 표시한다. */
  valueText?: string;
  sparkline?: number[];
  tone?: 'teal' | 'indigo' | 'amber' | 'rose';
  icon?: ReactNode;
  onClick?: () => void;
}

/** 4색 KPI 타일 — 라벨 + 카운트업 값 + 증감 화살표 + 스파크라인. onClick을 주면
 *  버튼으로, 없으면 정적 카드로 렌더한다(대시보드 개편에서 "카드 클릭 → 탭 이동"은
 *  유지하되 신규 위젯 배치는 다음 PR 몫이라 여기서는 콜백만 받는다). */
export function KpiTile({
  label,
  value,
  prevValue,
  formatValue = formatCompact,
  valueText,
  sparkline,
  tone = 'teal',
  icon,
  onClick,
}: KpiTileProps) {
  const animated = useCountUp(Number.isFinite(value) ? value : 0, 600);
  const displayText = valueText ?? formatValue(animated);
  const exactText = valueText ?? (Number.isFinite(value) ? Math.round(value).toLocaleString('en-US') : '—');

  const hasDelta = prevValue != null && Number.isFinite(prevValue) && Number.isFinite(value);
  const delta = hasDelta ? value - (prevValue as number) : 0;
  const deltaPct = hasDelta && prevValue !== 0 ? (delta / Math.abs(prevValue as number)) * 100 : undefined;
  const deltaDir = delta > 0 ? 'up' : delta < 0 ? 'down' : 'flat';

  const className = `chart-kpi chart-kpi-${tone}`;
  const ariaLabel = `${label} ${exactText}${hasDelta ? `, 직전 대비 ${deltaDir === 'up' ? '증가' : deltaDir === 'down' ? '감소' : '변동 없음'}` : ''}`;

  // 증감은 백분율을 낼 수 있을 때만 붙인다. prev가 0이면 "몇 % 늘었다"를 말할 수
  // 없어 화살표만 덩그러니 남는데, 그럴 바엔 빼는 편이 낫다.
  const showDelta = hasDelta && deltaDir !== 'flat' && deltaPct != null && Number.isFinite(deltaPct);

  const body = (
    <>
      <span className="chart-kpi-head">
        {icon && <span className="chart-kpi-icon">{icon}</span>}
        <span className="chart-kpi-label">{label}</span>
      </span>
      <span className="chart-kpi-main">
        <span className="chart-kpi-value" title={exactText}>
          {displayText}
        </span>
        {showDelta && (
          <span className={`chart-kpi-delta ${deltaDir}`}>
            <span className="chart-kpi-arrow" aria-hidden="true">
              {deltaDir === 'up' ? '▲' : '▼'}
            </span>
            {`${Math.abs(deltaPct as number).toFixed(0)}%`}
          </span>
        )}
        {sparkline && sparkline.length >= 2 && (
          <span className="chart-kpi-spark">
            <Sparkline data={sparkline} height={28} />
          </span>
        )}
      </span>
    </>
  );

  // 동적 태그명(button/div)을 JSX에서 그대로 쓰면 속성 타입 추론이 꼬여서
  // onClick 유무로 분기해 각각 명시적으로 렌더한다.
  if (onClick) {
    return (
      <button type="button" className={className} onClick={onClick} aria-label={ariaLabel}>
        {body}
      </button>
    );
  }
  return (
    <div className={className} aria-label={ariaLabel}>
      {body}
    </div>
  );
}
