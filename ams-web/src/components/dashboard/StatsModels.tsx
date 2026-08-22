'use client';

// 모델별 토큰 점유 도넛. StatsTimeseries의 by 토글과는 별개로 항상 model 축을
// 받아 구간 합계를 낸다(같은 SWR 키라 by=model로 보고 있을 땐 요청이 겹치지
// 않는다 — SWR이 캐시를 공유한다).
import { useMemo } from 'react';
import { Donut } from '@/components/charts';
import { useStatsTimeseries } from './useStats';
import type { StatsRange } from '@/lib/api-client/types';

/** 범례에 "claude-haiku-4-5-20251001"이 그대로 들어가면 카드 폭을 다 잡아먹고
 *  잘린다. 공급자 접두와 날짜 스냅샷 suffix는 같은 목록 안에서 전부 겹치는
 *  정보라 접어도 구분이 흐려지지 않는다 → "haiku-4-5". */
export function shortModelLabel(label: string): string {
  const withoutDate = label.replace(/[-@]\d{6,8}$/, '');
  const withoutVendor = withoutDate.replace(/^(claude|anthropic|gemini|gpt|models)[-/]/i, '');
  return withoutVendor || label;
}

export function StatsModels({ tenantId, range }: { tenantId: string; range: StatsRange }) {
  const { data } = useStatsTimeseries(tenantId, range, 'model');

  const values = useMemo(() => {
    const series = data?.series ?? [];
    return series
      .map((s) => ({
        key: s.key,
        label: s.key === 'other' ? s.label : shortModelLabel(s.label),
        value: s.values.reduce((a, b) => a + b, 0),
      }))
      .sort((a, b) => (a.key === 'other' ? 1 : b.key === 'other' ? -1 : b.value - a.value));
  }, [data]);

  return (
    <section className="panel dash-card dash-span-4">
      <div className="dash-card-head">
        <h2>모델별 토큰 점유</h2>
      </div>
      <div className="dash-card-body">
        <Donut values={values} unit="토큰" />
      </div>
    </section>
  );
}
