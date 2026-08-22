'use client';

// 계정 잔여 사용량. 기존 ['accounts', tenantId] SWR(PR #147, Account.usage)을
// 그대로 재사용한다 — stats/accounts에도 remaining5HPct 필드가 있지만 리셋
// 시각까지 주는 건 이 SWR의 원본 usage 객체뿐이라 여기서는 이쪽을 쓴다(같은
// 키라 다른 탭의 폴링과 중복되지 않는다).
//
// 배치는 계정당 한 줄(이메일 + 게이지 두 개)이다. 예전 카드 격자는 계정이
// 두엇뿐일 때 왼쪽에만 몰리고 오른쪽이 통째로 비었다.
import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import { RingGauge } from '@/components/charts';
import { fmtRemainingWindow } from '@/lib/usage-format';
import type { AccountPage, AccountUsageWindowSummary } from '@/lib/api-client/types';

const MAX_ROWS = 6;

function Window({ label, w }: { label: string; w: AccountUsageWindowSummary | null | undefined }) {
  return (
    <span className="dash-remain-cell">
      <RingGauge pct={w?.pct ?? NaN} windowLabel={label} size={44} strokeWidth={5} compact />
      <span className="dash-remain-meta">
        <span className="dash-remain-win">{label}</span>
        <span className="dash-remain-text">{fmtRemainingWindow(w)}</span>
      </span>
    </span>
  );
}

export function StatsRemaining({ tenantId }: { tenantId: string }) {
  const { data } = useSWR<AccountPage>(['accounts', tenantId], () => api.listAccounts(tenantId));
  const accounts = data?.items ?? [];
  const shown = accounts.slice(0, MAX_ROWS);
  const rest = accounts.length - shown.length;

  return (
    <section className="panel dash-card dash-span-4">
      <div className="dash-card-head">
        <h2>계정 잔여 사용량</h2>
      </div>
      <div className="dash-card-body">
        {accounts.length === 0 ? (
          <div className="chart-empty" role="img" aria-label="잔여 사용량 데이터 없음">
            등록된 계정이 없습니다.
          </div>
        ) : (
          <ul className="dash-remain-list">
            {shown.map((a) => (
              <li key={a.id} className="dash-remain-row">
                <span className="dash-remain-email" title={a.email}>{a.email}</span>
                <span className="dash-remain-gauges">
                  <Window label="5시간" w={a.usage?.fiveHour} />
                  <Window label="7일" w={a.usage?.sevenDay} />
                </span>
              </li>
            ))}
            {rest > 0 && <li className="dash-remain-more">외 {rest}개</li>}
          </ul>
        )}
      </div>
    </section>
  );
}
