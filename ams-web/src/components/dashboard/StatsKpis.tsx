'use client';

// KPI 4종(토큰·비용·세션·경보). stats/summary 하나로 전부 그린다. 클릭하면 해당
// 탭으로 이동한다(대시보드에 허용된 유일한 조작). 비용은 usage/cost 당월 합계라
// range와 무관하므로 라벨에 "이번 달"을 못박아 혼동을 막는다.
import { KpiTile } from '@/components/charts';
import { useStatsSummary } from './useStats';
import type { StatsRange } from '@/lib/api-client/types';

/** 비용은 서버가 Decimal을 문자열로 내려보낸다. 증감을 계산하려면 숫자가
 *  필요하지만 표시는 원문 문자열 그대로 써야 자릿수가 보존된다. */
function num(s: string | undefined): number {
  const v = Number(s);
  return Number.isFinite(v) ? v : NaN;
}

export function StatsKpis({
  tenantId,
  range,
  onGo,
}: {
  tenantId: string;
  range: StatsRange;
  onGo: (tab: string) => void;
}) {
  const { data } = useStatsSummary(tenantId, range);

  const tokens = data?.tokens ?? { value: 0, prev: 0 };
  const sessions = data?.sessions ?? { value: 0, prev: 0 };
  const alertsOpened = data?.alertsOpened ?? { value: 0, prev: 0 };
  const cost = data?.cost;
  const costValue = num(cost?.value);
  const costPrev = num(cost?.prev);

  return (
    <div className="dash-kpis">
      <KpiTile
        label="토큰"
        value={tokens.value}
        prevValue={data ? tokens.prev : undefined}
        sparkline={data?.sparkline.tokens}
        tone="teal"
        onClick={() => onGo('usage')}
      />
      <KpiTile
        label="이번 달 비용"
        value={costValue}
        prevValue={Number.isFinite(costPrev) ? costPrev : undefined}
        valueText={cost ? `${cost.value} ${cost.currency}` : '—'}
        tone="indigo"
        onClick={() => onGo('usage')}
      />
      <KpiTile
        label="세션"
        value={sessions.value}
        prevValue={data ? sessions.prev : undefined}
        sparkline={data?.sparkline.sessions}
        tone="amber"
        onClick={() => onGo('usage')}
      />
      <KpiTile
        label={`경보 발생 (열림 ${data?.alertsOpenNow ?? 0}건)`}
        value={alertsOpened.value}
        prevValue={data ? alertsOpened.prev : undefined}
        tone="rose"
        onClick={() => onGo('alerts')}
      />
    </div>
  );
}
