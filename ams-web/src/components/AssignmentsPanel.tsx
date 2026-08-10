'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { allowedAssignmentActions, api } from '@/lib/api-client/client';
import type { AssignmentActionVerb as Verb } from '@/lib/api-client/client';
import type { AccountPage, AssignmentPage, ServerPage } from '@/lib/api-client/types';
import { Badge, Modal, useAction } from './common';

const POLL = 6000;

const VERB_LABEL: Record<Verb, string> = {
  deliver: '전달',
  activate: '활성화',
  deactivate: '비활성화',
  recover: '복구',
  'switch-now': '즉시 전환',
  recall: '회수',
};

export function AssignmentsPanel({ tenantId }: { tenantId: string }) {
  const { data, mutate } = useSWR<AssignmentPage>(
    ['assignments', tenantId],
    () => api.listAssignments(tenantId),
    { refreshInterval: POLL },
  );
  // 계정·서버 목록을 재사용해 UUID를 이메일 → 서버명으로 표시한다(같은 SWR 키).
  const { data: accounts } = useSWR<AccountPage>(['accounts', tenantId], () => api.listAccounts(tenantId));
  const { data: servers } = useSWR<ServerPage>(['servers', tenantId], () => api.listServers(tenantId));
  const emailOf = new Map((accounts?.items ?? []).map((a) => [a.id, a.email]));
  const serverNameOf = new Map((servers?.items ?? []).map((s) => [s.id, s.name]));
  const [creating, setCreating] = useState(false);
  const act = useAction();
  const items = data?.items ?? [];

  async function doAction(id: string, verb: Verb) {
    await act.run(() => api.assignmentAction(tenantId, id, verb), () => mutate());
  }

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>할당</h2>
        <button className="primary" onClick={() => setCreating(true)}>계정 할당</button>
      </div>
      {act.error && <p className="err">{act.error}</p>}
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>파이프라인</th><th>상태</th><th>동기화</th><th>동작</th></tr>
          </thead>
          <tbody>
            {items.map((a) => {
              const allowed = allowedAssignmentActions(a.state);
              const email = emailOf.get(a.accountId) ?? a.accountId.slice(0, 8);
              const serverName = serverNameOf.get(a.serverId) ?? a.serverId.slice(0, 8);
              return (
                <tr key={a.id}>
                  <td>
                    <span className="pipeline">
                      <span>{email}</span>
                      <span className="arrow">→</span>
                      <span>{serverName}</span>
                    </span>
                  </td>
                  <td>
                    <Badge value={a.state} />
                    {a.lastError && <div className="err">{a.lastError}</div>}
                  </td>
                  <td className="muted">{a.pendingCommandId ? '동기화 중…' : '—'}</td>
                  <td>
                    <div className="actions">
                      {allowed.map((v) => (
                        <button key={v} disabled={act.busy} onClick={() => doAction(a.id, v)}>
                          {VERB_LABEL[v]}
                        </button>
                      ))}
                    </div>
                  </td>
                </tr>
              );
            })}
            {items.length === 0 && (
              <tr><td colSpan={4} className="muted">할당이 없습니다. '계정 할당'으로 계정을 서버에 연결하세요.</td></tr>
            )}
          </tbody>
        </table>
      </div>
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
  const act = useAction();
  return (
    <Modal title="계정을 서버에 할당" onClose={onClose}>
      <label>계정</label>
      <select value={accountId} onChange={(e) => setAccountId(e.target.value)}>
        <option value="">— 선택 —</option>
        {(accounts?.items ?? []).map((a) => <option key={a.id} value={a.id}>{a.email}</option>)}
      </select>
      <label>서버</label>
      <select value={serverId} onChange={(e) => setServerId(e.target.value)}>
        <option value="">— 선택 —</option>
        {(servers?.items ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
      </select>
      <p className="muted" style={{ marginTop: 12 }}>
        할당은 대기 상태로 생성됩니다. 목록에서 '전달'을 눌러 서버로 보내세요.
      </p>
      {act.error && <p className="err">{act.error}</p>}
      <button
        className="primary"
        style={{ marginTop: 14 }}
        disabled={act.busy || !accountId || !serverId}
        onClick={() =>
          act.run(
            () => api.createAssignment(tenantId, { accountId, serverId }),
            onDone,
          )
        }
      >
        할당 생성
      </button>
    </Modal>
  );
}
