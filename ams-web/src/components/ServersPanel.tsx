'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import type { EnrollTokenResponse, Server, ServerPage, UsageSnapshot } from '@/lib/api-client/types';
import { Badge, Modal, fmtTime, useAction } from './common';

const POLL = 7000;

export function ServersPanel({ tenantId }: { tenantId: string }) {
  const { data, mutate } = useSWR<ServerPage>(
    ['servers', tenantId],
    () => api.listServers(tenantId),
    { refreshInterval: POLL },
  );
  const [creating, setCreating] = useState(false);
  const [usageOf, setUsageOf] = useState<Server | null>(null);
  const [tokenOf, setTokenOf] = useState<EnrollTokenResponse | null>(null);
  const act = useAction();
  const servers = data?.items ?? [];

  return (
    <div className="panel">
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <h2>Servers</h2>
        <button className="primary" onClick={() => setCreating(true)}>+ Register server</button>
      </div>
      {act.error && <p className="err">{act.error}</p>}
      <table>
        <thead>
          <tr>
            <th>Name</th><th>Status</th><th>Mode</th><th>Accounts</th>
            <th>Last seen</th><th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {servers.map((s) => (
            <tr key={s.id}>
              <td>{s.name}<div className="muted">{s.hostname}</div></td>
              <td><Badge value={s.status} /></td>
              <td>{s.switchMode}</td>
              <td>{s.assignedAccountCount ?? 0}</td>
              <td className="muted">{fmtTime(s.lastSeenAt)}</td>
              <td>
                <div className="actions">
                  <button
                    disabled={act.busy}
                    onClick={() =>
                      act.run(
                        () => api.setSwitchMode(tenantId, s.id, s.switchMode === 'auto' ? 'manual' : 'auto'),
                        () => mutate(),
                      )
                    }
                  >
                    {s.switchMode === 'auto' ? 'Set manual' : 'Set auto'}
                  </button>
                  <button disabled={act.busy} onClick={() => act.run(() => api.refreshUsage(tenantId, s.id))}>
                    Refresh usage
                  </button>
                  <button onClick={() => setUsageOf(s)}>Usage</button>
                  <button
                    disabled={act.busy}
                    onClick={() =>
                      act.run(async () => {
                        const t = await api.issueEnrollToken(tenantId, s.id);
                        setTokenOf(t);
                      })
                    }
                  >
                    Enroll token
                  </button>
                  <button
                    className="danger"
                    disabled={act.busy}
                    onClick={() => act.run(() => api.deleteServer(tenantId, s.id), () => mutate())}
                  >
                    Delete
                  </button>
                </div>
              </td>
            </tr>
          ))}
          {servers.length === 0 && <tr><td colSpan={6} className="muted">No servers.</td></tr>}
        </tbody>
      </table>

      {creating && (
        <CreateServer tenantId={tenantId} onClose={() => setCreating(false)} onDone={() => { setCreating(false); mutate(); }} />
      )}
      {usageOf && <UsageModal tenantId={tenantId} server={usageOf} onClose={() => setUsageOf(null)} />}
      {tokenOf && (
        <Modal title="Enrollment token (shown once)" onClose={() => setTokenOf(null)}>
          <p className="muted">Copy now — it cannot be retrieved again.</p>
          <p><code>{tokenOf.token}</code></p>
          <p className="muted">Expires {fmtTime(tokenOf.expiresAt)}</p>
        </Modal>
      )}
    </div>
  );
}

function CreateServer({
  tenantId,
  onClose,
  onDone,
}: {
  tenantId: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const [name, setName] = useState('');
  const [hostname, setHostname] = useState('');
  const [mode, setMode] = useState<'auto' | 'manual'>('manual');
  const act = useAction();
  return (
    <Modal title="Register AMA server" onClose={onClose}>
      <label>Name</label>
      <input value={name} onChange={(e) => setName(e.target.value)} />
      <label>Hostname</label>
      <input value={hostname} onChange={(e) => setHostname(e.target.value)} />
      <label>Switch mode</label>
      <select value={mode} onChange={(e) => setMode(e.target.value as 'auto' | 'manual')}>
        <option value="manual">manual</option>
        <option value="auto">auto</option>
      </select>
      {act.error && <p className="err">{act.error}</p>}
      <button
        className="primary"
        style={{ marginTop: 14 }}
        disabled={act.busy || !name}
        onClick={() =>
          act.run(() => api.createServer(tenantId, { name, hostname: hostname || undefined, switchMode: mode }), onDone)
        }
      >
        Create
      </button>
    </Modal>
  );
}

function UsageModal({
  tenantId,
  server,
  onClose,
}: {
  tenantId: string;
  server: Server;
  onClose: () => void;
}) {
  const { data, error } = useSWR<UsageSnapshot>(
    ['usage', tenantId, server.id],
    () => api.getUsage(tenantId, server.id),
    { refreshInterval: POLL },
  );
  const p = data?.payload;
  return (
    <Modal title={`Usage — ${server.name}`} onClose={onClose}>
      {error && <p className="muted">No usage report yet.</p>}
      {p && (
        <>
          <p>
            Pool max utilization: <b>{p.poolSummary?.maxUtilizationPct ?? '—'}%</b>{' '}
            {p.poolSummary?.allExhausted && <Badge value="critical" />}
          </p>
          <p className="muted">Reported {fmtTime(data?.reportedAt)}</p>
          <table>
            <thead><tr><th>Email</th><th>Status</th><th>Current</th></tr></thead>
            <tbody>
              {(p.accounts ?? []).map((a) => (
                <tr key={a.amsAccountId}>
                  <td>{a.email}</td>
                  <td><Badge value={a.allocationStatus} /></td>
                  <td>{a.isCurrent ? '★' : ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {p.drift && p.drift.length > 0 && (
            <>
              <h3 style={{ marginTop: 12 }}>Drift</h3>
              <ul>{p.drift.map((d, i) => <li key={i} className="err">{d.email || d.amsAccountId}: {d.detail}</li>)}</ul>
            </>
          )}
        </>
      )}
    </Modal>
  );
}
