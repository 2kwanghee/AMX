'use client';

import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import type { AlertPage } from '@/lib/api-client/types';
import { Badge, fmtTime, useAction } from './common';

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
  const items = data?.items ?? [];

  return (
    <div className="panel">
      <h2>Alerts</h2>
      {error && (
        <p className="err">
          Could not load alerts: {error instanceof Error ? error.message : 'request failed'}.
        </p>
      )}
      {act.error && <p className="err">{act.error}</p>}
      {isLoading && <p className="muted">Loading…</p>}
      {!isLoading && items.length === 0 && !error && <p className="muted">No alerts.</p>}
      {items.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Severity</th>
              <th>Kind</th>
              <th>Status</th>
              <th>Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.map((a) => (
              <tr key={a.id}>
                <td><Badge value={a.severity} /></td>
                <td>{a.kind}</td>
                <td><Badge value={a.status} /></td>
                <td className="muted">{fmtTime(a.createdAt)}</td>
                <td>
                  <button
                    disabled={a.status !== 'open' || act.busy}
                    onClick={() => act.run(() => api.ackAlert(tenantId, a.id), () => mutate())}
                  >
                    Ack
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
