'use client';

// 서버→계정 시간 점유 흐름. stats/flows 응답이 이미 Sankey 프리미티브가 기대하는
// 모양(nodes: {id,kind,label}, links: {source,target,value})이라 그대로 넘긴다.
import { Sankey } from '@/components/charts';
import { useStatsFlows } from './useStats';
import type { StatsRange } from '@/lib/api-client/types';

export function StatsFlows({ tenantId, range }: { tenantId: string; range: StatsRange }) {
  const { data } = useStatsFlows(tenantId, range);
  const nodes = data?.nodes ?? [];
  const links = data?.links ?? [];

  return (
    <div className="panel">
      <h2>서버 → 계정 점유 흐름</h2>
      {links.length > 0 ? (
        <Sankey nodes={nodes} links={links} unit="초" />
      ) : (
        <div className="chart-empty" role="img" aria-label="흐름 데이터 없음">
          아직 집계된 점유 흐름이 없습니다.
        </div>
      )}
    </div>
  );
}
