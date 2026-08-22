'use client';

import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import type { EventPage, Server } from '@/lib/api-client/types';
import { formatEventRow } from '@/lib/event-format';
import { Badge, Modal, fmtTime } from '../common';
import { POLL } from './constants';

// E2 — switch/quarantine/all_exhausted timeline. Rows arrive most recent first,
// as ams-server orders them; formatEventRow (src/lib/event-format.ts) turns each
// raw AccountEvent payload into display fields.
export function EventsModal({
  tenantId,
  server,
  onClose,
}: {
  tenantId: string;
  server: Server;
  onClose: () => void;
}) {
  const { data, error } = useSWR<EventPage>(
    ['events', tenantId, server.id],
    () => api.listServerEvents(tenantId, server.id),
    { refreshInterval: POLL },
  );
  const events = data?.items ?? [];
  return (
    <Modal title={`이벤트 — ${server.name}`} onClose={onClose}>
      {error && <p className="muted">이벤트를 불러올 수 없습니다.</p>}
      {!error && events.length === 0 && <p className="muted">아직 이벤트가 없습니다.</p>}
      {events.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>시각</th><th>이벤트</th><th>이전 → 이후</th><th>상세</th></tr>
            </thead>
            <tbody>
              {events.map((ev, i) => {
                const row = formatEventRow(ev);
                return (
                  <tr key={ev.id ?? i}>
                    <td className="muted">{fmtTime(row.reportedAt)}</td>
                    <td>
                      <Badge value={row.kind} />
                      {row.trigger && <span className="muted"> {row.trigger}</span>}
                    </td>
                    <td>{row.transition ?? '—'}</td>
                    <td className="muted">{row.detail}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Modal>
  );
}
