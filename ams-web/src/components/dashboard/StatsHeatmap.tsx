'use client';

// 요일×시간 세션 히트맵. stats/heatmap의 cells를 그대로 넘긴다(요일 0=월요일,
// 시간은 UTC 0~23시 — Heatmap 컴포넌트가 이미 그 순서로 라벨을 붙인다).
import { Heatmap } from '@/components/charts';
import { useStatsHeatmap } from './useStats';
import type { StatsRange } from '@/lib/api-client/types';

export function StatsHeatmap({ tenantId, range }: { tenantId: string; range: StatsRange }) {
  const { data } = useStatsHeatmap(tenantId, range);

  return (
    <div className="panel">
      <h2>요일 · 시간대별 세션</h2>
      <Heatmap cells={data?.cells ?? []} />
    </div>
  );
}
