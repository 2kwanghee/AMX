'use client';

// 누적 영역 시계열 — 토큰(모델별) / 시간 점유(서버별)를 토글로 바꿔 본다. 이
// 토글이 이번 화면에서 허용된 두 조작 중 하나다(다른 하나는 상위 기간 선택).
// unit이 tokens면 session_usage(세션 종료 시각) 집계라 범례를 붙인다 — 서버축은
// rollup(일 단위 적분)이라 이 제약이 없다.
import { useState } from 'react';
import { AreaChart } from '@/components/charts';
import { useStatsTimeseries } from './useStats';
import type { StatsRange, StatsTimeseriesBy } from '@/lib/api-client/types';

export function StatsTimeseries({ tenantId, range }: { tenantId: string; range: StatsRange }) {
  const [by, setBy] = useState<StatsTimeseriesBy>('model');
  const { data } = useStatsTimeseries(tenantId, range, by);

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>사용량 추이</h2>
        <div className="actions">
          <label className="muted" style={{ fontSize: 12 }}>
            기준
            <select value={by} onChange={(e) => setBy(e.target.value as StatsTimeseriesBy)} style={{ marginLeft: 4 }}>
              <option value="model">모델별 토큰</option>
              <option value="server">서버별 점유</option>
            </select>
          </label>
        </div>
      </div>
      <AreaChart buckets={data?.buckets ?? []} series={data?.series ?? []} unit={data?.unit ?? 'tokens'} />
      {data?.unit === 'tokens' && (
        <p className="muted" style={{ fontSize: 11, marginTop: 6 }}>
          세션 종료 시각 기준 집계입니다.
        </p>
      )}
    </div>
  );
}
