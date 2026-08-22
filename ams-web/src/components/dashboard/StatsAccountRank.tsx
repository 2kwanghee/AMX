'use client';

// 계정 토큰 순위 바 + 부제(최다 모델·프로젝트·서버). RankBars는 라벨 칸이 96px
// 고정폭 한 줄이라 계정별 부가정보까지 한 행에 욱여넣을 자리가 없다(charts는
// 이번 작업에서 수정 금지 대상이라 그대로 둔다) — 그래서 순위 바 밑에 같은
// 순서로 부제 목록을 별도로 붙인다. 상위 8개만 보여준다(50행 전부는 위젯
// 하나에 과하다).
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
    () => rows.map((r) => ({ key: r.accountId, label: r.email ?? r.accountId.slice(0, 8), value: r.tokens })),
    [rows],
  );

  return (
    <div className="panel">
      <h2>계정 토큰 순위</h2>
      <RankBars rows={bars} unit="" />
      {rows.length > 0 && (
        <ul className="dash-rank-notes">
          {rows.map((r) => (
            <li key={r.accountId}>
              <span className="dash-rank-notes-email">{r.email ?? r.accountId.slice(0, 8)}</span>
              <span className="muted">{subtitleOf(r)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
