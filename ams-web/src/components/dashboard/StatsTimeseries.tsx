'use client';

// 누적 영역 시계열 — 토큰(모델별) / 시간 점유(서버별)를 토글로 바꿔 본다. 이
// 토글이 이번 화면에서 허용된 두 조작 중 하나다(다른 하나는 상위 기간 선택).
// unit이 tokens면 session_usage(세션 종료 시각) 집계라 그 사실을 부제로 붙인다 —
// 서버축은 rollup(일 단위 적분)이라 이 제약이 없다.
import { useState } from 'react';
import { AreaChart } from '@/components/charts';
import { useStatsTimeseries } from './useStats';
import type { StatsRange, StatsTimeseriesBy } from '@/lib/api-client/types';

export function StatsTimeseries({ tenantId, range }: { tenantId: string; range: StatsRange }) {
  const [by, setBy] = useState<StatsTimeseriesBy>('model');
  const { data } = useStatsTimeseries(tenantId, range, by);

  return (
    <section className="panel dash-card dash-span-8">
      <div className="dash-card-head">
        <h2>
          사용량 추이
          {data?.unit === 'tokens' && <span className="dash-card-sub">세션 종료 시각 기준</span>}
        </h2>
        <label className="dash-card-select">
          기준
          <select value={by} onChange={(e) => setBy(e.target.value as StatsTimeseriesBy)}>
            <option value="model">모델별 토큰</option>
            <option value="server">서버별 점유</option>
          </select>
        </label>
      </div>
      <div className="dash-card-body">
        <AreaChart buckets={data?.buckets ?? []} series={data?.series ?? []} unit={data?.unit ?? 'tokens'} height={220} />
      </div>
    </section>
  );
}
