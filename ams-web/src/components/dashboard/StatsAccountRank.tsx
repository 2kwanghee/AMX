'use client';

// 계정 토큰 순위 바. 부제(최다 모델·프로젝트·서버)는 RankBars가 sub 필드로 행
// 안에 받아 라벨 밑에 한 줄로 붙인다 — 예전처럼 바 목록과 부제 목록을 따로 두면
// 같은 계정을 두 번 읽어야 하고, 카드 아래쪽에 빈 공간이 남는다. 상위 8개만
// 보여준다(50행 전부는 위젯 하나에 과하다).
import { useMemo } from 'react';
import { RankBars } from '@/components/charts';
import { useStatsAccounts } from './useStats';
import type { StatsAccountRow, StatsRange } from '@/lib/api-client/types';

const TOP_N = 8;

function subtitleOf(row: StatsAccountRow): string {
  const parts = [row.topModel, row.topProject, row.topServerName].filter(
    (v): v is string => Boolean(v && v.trim()),
  );
  return parts.length > 0 ? parts.join(' · ') : '집계된 사용 내역 없음';
}

export function StatsAccountRank({ tenantId, range }: { tenantId: string; range: StatsRange }) {
  const { data } = useStatsAccounts(tenantId, range);
  const rows = useMemo(() => (data?.rows ?? []).slice(0, TOP_N), [data]);

  const bars = useMemo(
    () =>
      rows.map((r) => ({
        key: r.accountId,
        label: r.email ?? r.accountId.slice(0, 8),
        value: r.tokens,
        sub: subtitleOf(r),
      })),
    [rows],
  );

  return (
    <section className="panel dash-card dash-span-4">
      <div className="dash-card-head">
        <h2>계정 토큰 순위</h2>
      </div>
      <div className="dash-card-body">
        <RankBars rows={bars} unit="" />
      </div>
    </section>
  );
}
