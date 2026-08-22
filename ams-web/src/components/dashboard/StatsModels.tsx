'use client';

// 모델별 토큰 점유 도넛. StatsTimeseries의 by 토글과는 별개로 항상 model 축을
// 받아 구간 합계를 낸다(같은 SWR 키라 by=model로 보고 있을 땐 요청이 겹치지
// 않는다 — SWR이 캐시를 공유한다).
import { useMemo } from 'react';
import { Donut } from '@/components/charts';
import { useStatsTimeseries } from './useStats';
import type { StatsRange } from '@/lib/api-client/types';

export function StatsModels({ tenantId, range }: { tenantId: string; range: StatsRange }) {
  const { data } = useStatsTimeseries(tenantId, range, 'model');

  const values = useMemo(() => {
    const series = data?.series ?? [];
    return series
      .map((s) => ({ key: s.key, label: s.label, value: s.values.reduce((a, b) => a + b, 0) }))
      .sort((a, b) => (a.key === 'other' ? 1 : b.key === 'other' ? -1 : b.value - a.value));
  }, [data]);

  return (
    <div className="panel">
      <h2>모델별 토큰 점유</h2>
      <Donut values={values} unit="토큰" />
    </div>
  );
}
