'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { allowedAssignmentActions, api } from '@/lib/api-client/client';
import type { AssignmentActionVerb as Verb } from '@/lib/api-client/client';
import type { AccountPage, AssignmentPage, ServerPage } from '@/lib/api-client/types';
import { Badge, Modal, useAction } from './common';

const POLL = 6000;
const ALL_VERBS: Verb[] = ['deliver', 'activate', 'deactivate', 'recover', 'switch-now', 'recall'];

export function AssignmentsPanel({ tenantId }: { tenantId: string }) {
  const { data, mutate } = useSWR<AssignmentPage>(
    ['assignments', tenantId],
    () => api.listAssignments(tenantId),
    { refreshInterval: POLL },
  );
  const [creating, setCreating] = useState(false);
  const act = useAction();
  const items = data?.items ?? [];

  async function doAction(id: string, verb: Verb) {
    await act.run(() => api.assignmentAction(tenantId, id, verb), () => mutate());
  }

  return (
    <div className="panel">
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <h2>Assignments</h2>
        <button className="primary" onClick={() => setCreating(true)}>+ Assign account</button>
      </div>
      {act.error && <p className="err">{act.error}</p>}
      <table>
        <thead>
          <tr><th>Account</th><th>Server</th><th>State</th><th>Pending</th><th>Actions</th></tr>
        </thead>
        <tbody>
          {items.map((a) => {
            const allowed = allowedAssignmentActions(a.state);
            return (
              <tr key={a.id}>
                <td className="muted" title={a.accountId}>{a.accountId.slice(0, 8)}</td>
                <td className="muted" title={a.serverId}>{a.serverId.slice(0, 8)}</td>
                <td>
                  <Badge value={a.state} />
                  {a.lastError && <div className="err">{a.lastError}</div>}
                </td>
                <td className="muted">{a.pendingCommandId ? '⏳ converging' : '—'}</td>
                <td>
                  <div className="actions">
                    {ALL_VERBS.map((v) => (
                      <button
                        key={v}
                        disabled={act.busy || !allowed.includes(v)}
                        onClick={() => doAction(a.id, v)}
                      >
                        {v}
                      </button>
                    ))}
                  </div>
                </td>
              </tr>
            );
          })}
          {items.length === 0 && <tr><td colSpan={5} className="muted">No assignments.</td></tr>}
        </tbody>
      </table>
      {creating && (
        <CreateAssignment
          tenantId={tenantId}
          onClose={() => setCreating(false)}
          onDone={() => { setCreating(false); mutate(); }}
        />
      )}
    </div>
  );
}

function CreateAssignment({
  tenantId,
  onClose,
  onDone,
}: {
  tenantId: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const { data: accounts } = useSWR<AccountPage>(['accounts', tenantId], () => api.listAccounts(tenantId));
  const { data: servers } = useSWR<ServerPage>(['servers', tenantId], () => api.listServers(tenantId));
  const [accountId, setAccountId] = useState('');
  const [serverId, setServerId] = useState('');
  const [deliver, setDeliver] = useState(false);
  const act = useAction();
  return (
    <Modal title="Assign account to server" onClose={onClose}>
      <label>Account</label>
      <select value={accountId} onChange={(e) => setAccountId(e.target.value)}>
        <option value="">— select —</option>
        {(accounts?.items ?? []).map((a) => <option key={a.id} value={a.id}>{a.email}</option>)}
      </select>
      <label>Server</label>
      <select value={serverId} onChange={(e) => setServerId(e.target.value)}>
        <option value="">— select —</option>
        {(servers?.items ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
      </select>
      <label style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 10 }}>
        <input type="checkbox" style={{ width: 'auto' }} checked={deliver} onChange={(e) => setDeliver(e.target.checked)} />
        Deliver immediately
      </label>
      {act.error && <p className="err">{act.error}</p>}
      <button
        className="primary"
        style={{ marginTop: 14 }}
        disabled={act.busy || !accountId || !serverId}
        onClick={() =>
          act.run(
            () => api.createAssignment(tenantId, { accountId, serverId, deliverImmediately: deliver }),
            onDone,
          )
        }
      >
        Create
      </button>
    </Modal>
  );
}
