'use client';

// 대시보드 홈 — 집계 통계 전용 화면(design-notes/dashboard-redesign-plan.md §4).
// 조작 요소는 기간 선택과 각 위젯의 by 토글, "탭으로 이동" 클릭뿐이다. 기간은
// 이 컴포넌트가 들고 각 위젯에 내려준다 — 위젯마다 따로 select를 두면 화면
// 전체가 서로 다른 구간을 보여줄 수 있어서다.
//
// 배치는 12컬럼 그리드 하나(.dash-grid)로 통일한다. 위젯이 각자 .row에 흩어져
// 있으면 같은 줄에 놓인 카드끼리 높이가 어긋나고 간격도 제각각이 된다 —
// 그리드로 모으면 span 값만 보고 어느 카드가 몇 칸인지 읽을 수 있다.
import { useState } from 'react';
import type { StatsRange } from '@/lib/api-client/types';
import { StatsKpis } from './StatsKpis';
import { StatsTimeseries } from './StatsTimeseries';
import { StatsFlows } from './StatsFlows';
import { StatsModels } from './StatsModels';
import { StatsAccountRank } from './StatsAccountRank';
import { StatsRemaining } from './StatsRemaining';
import { StatsHeatmap } from './StatsHeatmap';
import { StatsTimeline } from './StatsTimeline';

const RANGES: { value: StatsRange; label: string }[] = [
  { value: '24h', label: '최근 24시간' },
  { value: '7d', label: '최근 7일' },
  { value: '30d', label: '최근 30일' },
];

export function DashboardHome({ tenantId, onGo }: { tenantId: string; onGo: (tab: string) => void }) {
  const [range, setRange] = useState<StatsRange>('7d');

  return (
    <>
      <div className="dash-range-row">
        <label className="muted" style={{ fontSize: 12 }}>
          기간
          <select value={range} onChange={(e) => setRange(e.target.value as StatsRange)} style={{ marginLeft: 4 }}>
            {RANGES.map((r) => (
              <option key={r.value} value={r.value}>{r.label}</option>
            ))}
          </select>
        </label>
      </div>

      <div className="dash-grid">
        <StatsKpis tenantId={tenantId} range={range} onGo={onGo} />
        <StatsTimeseries tenantId={tenantId} range={range} />
        <StatsModels tenantId={tenantId} range={range} />
        <StatsFlows tenantId={tenantId} range={range} />
        <StatsAccountRank tenantId={tenantId} range={range} />
        <StatsRemaining tenantId={tenantId} />
        <StatsHeatmap tenantId={tenantId} range={range} />
        <StatsTimeline tenantId={tenantId} />
      </div>
    </>
  );
}
