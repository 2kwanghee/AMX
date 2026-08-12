'use client';

import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import type { AlertPage, ServerPage } from '@/lib/api-client/types';
import { Badge, LiveDot, TimeCell, useAction, useMarkOnData } from './common';

const POLL = 7000;

// 경보 detail(JSON 객체)에서 사람이 읽을 사유 한 줄을 뽑는다. self_update_failed는
// detail 안에 error_code·detail/message를 담아 오므로 code: message로 합치고,
// 알 수 없는 형태면 통째로 JSON 문자열화한다(표에서 말줄임 + title 툴팁).
function detailText(detail?: Record<string, unknown>): string {
  if (!detail || Object.keys(detail).length === 0) return '';
  const s = (v: unknown) => (typeof v === 'string' && v.trim() ? v.trim() : undefined);
  const code = s(detail.error_code) ?? s(detail.code);
  const msg = s(detail.detail) ?? s(detail.message) ?? s(detail.reason);
  if (code && msg) return `${code}: ${msg}`;
  if (code) return code;
  if (msg) return msg;
  return JSON.stringify(detail);
}

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
  // 대상 서버명 해석용. 신규 SWR 키를 만들지 않고 ServersPanel과 동일한
  // ['servers', tenantId] 키를 재사용해 캐시를 공유한다.
  const { data: serversData } = useSWR<ServerPage>(
    tenantId ? ['servers', tenantId] : null,
    () => api.listServers(tenantId),
    { refreshInterval: POLL },
  );
  const act = useAction();
  useMarkOnData(data);
  const items = data?.items ?? [];
  const serverNameOf = new Map((serversData?.items ?? []).map((s) => [s.id, s.name]));
  const serverLabel = (id?: string) => (id ? serverNameOf.get(id) ?? id.slice(0, 8) : '—');

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
                <th>대상 서버</th>
                <th>사유</th>
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
                  <td>{serverLabel(a.serverId)}</td>
                  <td className="muted">
                    {(() => {
                      const txt = detailText(a.detail);
                      return txt ? (
                        <span
                          title={txt}
                          style={{ display: 'inline-block', maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', verticalAlign: 'bottom' }}
                        >
                          {txt}
                        </span>
                      ) : (
                        '—'
                      );
                    })()}
                  </td>
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
