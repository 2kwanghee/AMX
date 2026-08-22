'use client';

// 계정 잔여 사용량 원형게이지 격자. 기존 ['accounts', tenantId] SWR(PR #147,
// Account.usage)을 그대로 재사용한다 — stats/accounts에도 remaining5HPct 필드가
// 있지만 리셋 시각까지 주는 건 이 SWR의 원본 usage 객체뿐이라 여기서는 이쪽을
// 쓴다(같은 키라 다른 탭의 폴링과 중복되지 않는다).
import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import { RingGauge } from '@/components/charts';
import { fmtRemainingWindow } from '@/lib/usage-format';
import type { AccountPage } from '@/lib/api-client/types';

export function StatsRemaining({ tenantId }: { tenantId: string }) {
  const { data } = useSWR<AccountPage>(['accounts', tenantId], () => api.listAccounts(tenantId));
  const accounts = data?.items ?? [];

  return (
    <div className="panel">
      <h2>계정 잔여 사용량</h2>
      {accounts.length === 0 ? (
        <div className="chart-empty" role="img" aria-label="잔여 사용량 데이터 없음">
          등록된 계정이 없습니다.
        </div>
      ) : (
        <div className="dash-gauge-grid">
          {accounts.map((a) => (
            <div key={a.id} className="dash-gauge-card">
              <div className="dash-gauge-card-email" title={a.email}>{a.email}</div>
              <div className="dash-gauge-card-rings">
                <RingGauge
                  pct={a.usage?.fiveHour?.pct ?? NaN}
                  remainingText={a.usage?.fiveHour ? fmtRemainingWindow(a.usage.fiveHour) : undefined}
                  windowLabel="5시간"
                  size={72}
                  strokeWidth={7}
                />
                <RingGauge
                  pct={a.usage?.sevenDay?.pct ?? NaN}
                  remainingText={a.usage?.sevenDay ? fmtRemainingWindow(a.usage.sevenDay) : undefined}
                  windowLabel="7일"
                  size={72}
                  strokeWidth={7}
                />
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
