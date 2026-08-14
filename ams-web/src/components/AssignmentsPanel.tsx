'use client';

import { useState } from 'react';
import useSWR, { useSWRConfig } from 'swr';
import { allowedAssignmentActions, api } from '@/lib/api-client/client';
import type { AssignmentActionVerb as Verb } from '@/lib/api-client/client';
import type { Account, Assignment, AccountPage, AssignmentPage, ServerPage } from '@/lib/api-client/types';
import { Badge, EmailChip, Icon, LiveDot, Modal, TimeCell, useAction, useMarkOnData, type IconName } from './common';

const POLL = 6000;

const VERB_LABEL: Record<Verb, string> = {
  deliver: '전달',
  activate: '활성화',
  deactivate: '비활성화',
  recover: '복구',
  'switch-now': '즉시 전환',
  recall: '회수',
};

// verb별 아이콘 + 버튼 강조. 즉시 전환=액센트, 회수=warn, 나머지=소프트.
const VERB_ICON: Record<Verb, IconName> = {
  deliver: 'send',
  activate: 'power',
  deactivate: 'pause',
  recover: 'rotate',
  'switch-now': 'zap',
  recall: 'undo',
};
const VERB_STYLE: Partial<Record<Verb, string>> = {
  'switch-now': 'accent',
  recall: 'warn',
};

// 동기화 셀 — pendingCommandId는 명령이 날아가는 동안만 값이 있고 ack 시점에
// 비워지므로, 평상시엔 ackedAt/deliveredAt로 마지막 동기화 결과를 보여준다.
function SyncCell({ a }: { a: Assignment }) {
  if (a.pendingCommandId) {
    return (
      <span className="sync-cell syncing">
        <Icon name="rotate" size={12} />
        동기화 중…
      </span>
    );
  }
  if (a.lastError) {
    return (
      <span className="sync-cell err" title={a.lastError}>
        동기화 실패
      </span>
    );
  }
  if (a.ackedAt) {
    return (
      <span className="sync-cell ok">
        <Icon name="check" size={12} />
        동기화됨 <TimeCell iso={a.ackedAt} />
      </span>
    );
  }
  if (a.deliveredAt) {
    return (
      <span className="sync-cell">
        전달됨 <TimeCell iso={a.deliveredAt} />
      </span>
    );
  }
  return <span className="muted">대기</span>;
}

// 같은 서버에 할당된 계정 중 lastSwitchedAt이 가장 최신(non-null)인 계정을 그
// 서버의 "현재 활성"으로 판정한다. 반환: serverId -> 현재 활성 accountId.
export function currentActiveByServer(
  assignments: Assignment[],
  accounts: Account[],
): Map<string, string> {
  const switchedAt = new Map(accounts.map((a) => [a.id, a.lastSwitchedAt]));
  const best = new Map<string, { accountId: string; t: number }>();
  for (const a of assignments) {
    if (a.state === 'detached') continue; // 회수된 연결은 "현재 활성" 판정에서 제외
    const iso = switchedAt.get(a.accountId);
    if (!iso) continue;
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) continue;
    const cur = best.get(a.serverId);
    if (!cur || t > cur.t) best.set(a.serverId, { accountId: a.accountId, t });
  }
  const out = new Map<string, string>();
  for (const [serverId, v] of best) out.set(serverId, v.accountId);
  return out;
}

// 계정 → 서버 연결선. 활성 행은 점이 흐르고, 그 외는 정적 점선 화살표.
function PipeFlow({ active }: { active: boolean }) {
  return (
    <span className={`pipe-flow ${active ? 'flowing' : ''}`} aria-hidden="true">
      <svg width="46" height="14" viewBox="0 0 46 14">
        <line className="pipe-line" x1="2" y1="7" x2="40" y2="7" />
        <path className="pipe-head" d="M38 3l5 4-5 4" fill="none" />
        {active && (
          <>
            <circle className="pipe-dot d1" cx="0" cy="7" r="2.5" />
            <circle className="pipe-dot d2" cx="0" cy="7" r="2.5" />
            <circle className="pipe-dot d3" cx="0" cy="7" r="2.5" />
          </>
        )}
      </svg>
    </span>
  );
}

export function AssignmentsPanel({ tenantId }: { tenantId: string }) {
  const { data, mutate } = useSWR<AssignmentPage>(
    ['assignments', tenantId],
    () => api.listAssignments(tenantId),
    { refreshInterval: POLL },
  );
  // 계정·서버 목록을 재사용해 UUID를 이메일 → 서버명으로 표시한다(같은 SWR 키).
  const { data: accounts } = useSWR<AccountPage>(['accounts', tenantId], () => api.listAccounts(tenantId));
  const { data: servers } = useSWR<ServerPage>(['servers', tenantId], () => api.listServers(tenantId));
  const { mutate: globalMutate } = useSWRConfig();
  const emailOf = new Map((accounts?.items ?? []).map((a) => [a.id, a.email]));
  const serverNameOf = new Map((servers?.items ?? []).map((s) => [s.id, s.name]));
  const [creating, setCreating] = useState(false);
  const [flashId, setFlashId] = useState<string | null>(null);
  const act = useAction();
  useMarkOnData(data);
  const items = data?.items ?? [];
  const activeByServer = currentActiveByServer(items, accounts?.items ?? []);

  async function doAction(id: string, verb: Verb) {
    await act.run(
      () => api.assignmentAction(tenantId, id, verb),
      async () => {
        await mutate();
        if (verb === 'switch-now') {
          // 배지가 실데이터로 이동하도록 accounts도 재검증하고, 해당 행을 1회 플래시.
          setFlashId(id);
          setTimeout(() => setFlashId(null), 700);
          globalMutate(['accounts', tenantId]);
        }
      },
    );
  }

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>할당<LiveDot /></h2>
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
              const isActive = activeByServer.get(a.serverId) === a.accountId;
              const rowClass = `${isActive ? 'pipe-active' : ''} ${flashId === a.id ? 'flash' : ''} ${a.state === 'detached' ? 'detached-row' : ''}`.trim();
              return (
                <tr key={a.id} className={rowClass || undefined}>
                  <td>
                    <span className="pipeline">
                      <EmailChip email={email} />
                      <PipeFlow active={isActive} />
                      <span>{serverName}</span>
                      {isActive && (
                        <span className="active-badge"><span className="dot" />현재 활성</span>
                      )}
                    </span>
                  </td>
                  <td>
                    <Badge value={a.state} />
                    {a.lastError && <div className="err">{a.lastError}</div>}
                  </td>
                  <td><SyncCell a={a} /></td>
                  <td>
                    <div className="actions">
                      {allowed.map((v) => (
                        <button
                          key={v}
                          className={`vbtn ${VERB_STYLE[v] ?? ''}`.trim()}
                          disabled={act.busy}
                          onClick={() => doAction(a.id, v)}
                        >
                          <span className="vbtn-icon"><Icon name={VERB_ICON[v]} size={14} /></span>
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
