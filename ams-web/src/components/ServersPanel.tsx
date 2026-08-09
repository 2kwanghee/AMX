'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import type {
  EnrollTokenResponse,
  EventPage,
  Server,
  ServerPage,
  ServerUpdate,
  SwitchStrategy,
  UsageSnapshot,
} from '@/lib/api-client/types';
import { formatEventRow } from '@/lib/event-format';
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
  const [policyOf, setPolicyOf] = useState<Server | null>(null);
  const [eventsOf, setEventsOf] = useState<Server | null>(null);
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
                  <button onClick={() => setEventsOf(s)}>Events</button>
                  <button onClick={() => setPolicyOf(s)}>Policy</button>
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
      {eventsOf && <EventsModal tenantId={tenantId} server={eventsOf} onClose={() => setEventsOf(null)} />}
      {policyOf && (
        <PolicyModal
          tenantId={tenantId}
          server={policyOf}
          onClose={() => setPolicyOf(null)}
          onDone={() => { setPolicyOf(null); mutate(); }}
        />
      )}
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

// E1 — central policy editor (design §O4). Each numeric field is optional: a
// blank field is submitted as null, which clears the central override and lets
// the agent fall back to its tsamx-local default. Ranges mirror the server-side
// ServerUpdate validation so the UI blocks obviously bad input before the PATCH.
function PolicyModal({
  tenantId,
  server,
  onClose,
  onDone,
}: {
  tenantId: string;
  server: Server;
  onClose: () => void;
  onDone: () => void;
}) {
  const numStr = (n?: number | null) => (n === null || n === undefined ? '' : String(n));
  const [threshold, setThreshold] = useState(numStr(server.thresholdPct));
  const [strategy, setStrategy] = useState<'' | SwitchStrategy>(server.defaultStrategy ?? '');
  const [cooldown, setCooldown] = useState(numStr(server.cooldownSeconds));
  const [hysteresis, setHysteresis] = useState(numStr(server.hysteresisPct));
  const act = useAction();

  // '' -> null (clear override); otherwise the parsed number. Returns undefined
  // when the value is present but not a valid number in [min,max].
  function parse(v: string, min: number, max: number): number | null | undefined {
    if (v.trim() === '') return null;
    const n = Number(v);
    if (!Number.isFinite(n) || n < min || n > max) return undefined;
    return n;
  }

  function save() {
    const thresholdPct = parse(threshold, 0, 100);
    const cooldownSeconds = parse(cooldown, 0, 86400);
    const hysteresisPct = parse(hysteresis, 0, 50);
    if (thresholdPct === undefined) return act.setError('threshold must be 0–100');
    if (cooldownSeconds === undefined) return act.setError('cooldown must be 0–86400 seconds');
    if (hysteresisPct === undefined) return act.setError('hysteresis must be 0–50');
    const body: ServerUpdate = {
      thresholdPct,
      defaultStrategy: strategy === '' ? null : strategy,
      cooldownSeconds,
      hysteresisPct,
    };
    return act.run(() => api.updateServer(tenantId, server.id, body), onDone);
  }

  return (
    <Modal title={`Policy — ${server.name}`} onClose={onClose}>
      <p className="muted">Blank a field to clear the central override (agent uses its local default).</p>
      <label>Switch threshold (%)</label>
      <input value={threshold} onChange={(e) => setThreshold(e.target.value)} placeholder="e.g. 95" />
      <label>Default strategy</label>
      <select value={strategy} onChange={(e) => setStrategy(e.target.value as '' | SwitchStrategy)}>
        <option value="">(local default)</option>
        <option value="best">best</option>
        <option value="next_available">next_available</option>
      </select>
      <label>Cooldown (seconds)</label>
      <input value={cooldown} onChange={(e) => setCooldown(e.target.value)} placeholder="e.g. 300" />
      <label>Hysteresis (%)</label>
      <input value={hysteresis} onChange={(e) => setHysteresis(e.target.value)} placeholder="e.g. 5" />
      {act.error && <p className="err">{act.error}</p>}
      <button className="primary" style={{ marginTop: 14 }} disabled={act.busy} onClick={save}>
        Save policy
      </button>
    </Modal>
  );
}

// E2 — switch/quarantine/all_exhausted timeline. Rows arrive most recent first,
// as ams-server orders them; formatEventRow (src/lib/event-format.ts) turns each
// raw AccountEvent payload into display fields.
function EventsModal({
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
    <Modal title={`Events — ${server.name}`} onClose={onClose}>
      {error && <p className="muted">No events available.</p>}
      {!error && events.length === 0 && <p className="muted">No events yet.</p>}
      {events.length > 0 && (
        <table>
          <thead>
            <tr><th>When</th><th>Event</th><th>From → To</th><th>Detail</th></tr>
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
      )}
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
