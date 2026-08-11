'use client';

import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import type { AlertPage } from '@/lib/api-client/types';
import { Badge, LiveDot, TimeCell, useAction, useMarkOnData } from './common';

const POLL = 7000;

export function AlertsBadge({ tenantId }: { tenantId: string }) {
  const { data } = useSWR<AlertPage>(
    tenantId ? ['alerts', tenantId, 'open'] : null,
    () => api.listAlerts(tenantId, 'open'),
    { refreshInterval: POLL },
  );
  const open = data?.items?.length ?? 0;
  if (!open) return null;
  return <span className="alert-badge">{open}</span>;
}

export function AlertsPanel({ tenantId }: { tenantId: string }) {
  const { data, error, isLoading, mutate } = useSWR<AlertPage>(
    tenantId ? ['alerts', tenantId, 'all'] : null,
    () => api.listAlerts(tenantId),
    { refreshInterval: POLL },
  );
  const act = useAction();
  useMarkOnData(data);
  const items = data?.items ?? [];

  return (
    <div className="panel">
      <h2>알림<LiveDot /></h2>
      {error && (
        <p className="err">
          알림을 불러오지 못했습니다: {error instanceof Error ? error.message : '요청 실패'}.
        </p>
      )}
      {act.error && <p className="err">{act.error}</p>}
      {isLoading && <p className="muted">불러오는 중…</p>}
      {!isLoading && items.length === 0 && !error && <p className="muted">알림이 없습니다.</p>}
      {items.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>심각도</th>
                <th>종류</th>
                <th>상태</th>
                <th>발생 시각</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {items.map((a) => (
                <tr key={a.id}>
                  <td><Badge value={a.severity} /></td>
                  <td>{a.kind}</td>
                  <td><Badge value={a.status} /></td>
                  <td><TimeCell iso={a.createdAt} /></td>
                  <td>
                    <button
                      disabled={a.status !== 'open' || act.busy}
                      onClick={() => act.run(() => api.ackAlert(tenantId, a.id), () => mutate())}
                    >
                      확인
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
