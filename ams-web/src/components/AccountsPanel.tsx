'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import type { Account, AccountPage, AccountUsageSummary } from '@/lib/api-client/types';
import { fmtRemainingWindow } from '@/lib/usage-format';
import { DirectImport } from './accounts/DirectImportModal';
import { EditAccount } from './accounts/EditAccountModal';
import { RegisterModal } from './accounts/RegisterModal';
import { Badge, EmailChip, LiveDot, ProviderTag, TimeCell, krLabel, relTime, useAction, useMarkOnData } from './common';

const POLL = 8000;

// 잔여(5h/7d) 셀. 값은 usage-format.fmtRemainingWindow 로 "잔여 62% · 14:30 리셋"
// 한 줄씩 두 줄로 쌓는다. stale이면 흐림 처리하고, 마지막 관측 경과를 툴팁으로
// 붙인다(§2단계 — 값 자체는 숨기지 않는다).
function UsageCell({ usage }: { usage?: AccountUsageSummary | null }) {
  if (!usage) return <span className="muted">—</span>;
  const title = usage.stale && usage.fetchedAt ? `${relTime(usage.fetchedAt)} 관측` : undefined;
  return (
    <div className={`account-usage-cell${usage.stale ? ' muted' : ''}`} title={title}>
      <span>5h {fmtRemainingWindow(usage.fiveHour)}</span>
      <span>7d {fmtRemainingWindow(usage.sevenDay)}</span>
    </div>
  );
}

export function AccountsPanel({ tenantId }: { tenantId: string }) {
  const { data, mutate } = useSWR<AccountPage>(
    ['accounts', tenantId],
    () => api.listAccounts(tenantId),
    { refreshInterval: POLL },
  );
  const [wizard, setWizard] = useState(false);
  const [direct, setDirect] = useState(false);
  const [editing, setEditing] = useState<Account | null>(null);
  const act = useAction();
  useMarkOnData(data);
  const accounts = data?.items ?? [];

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>계정<LiveDot /></h2>
        <div className="actions">
          <button className="primary" onClick={() => setWizard(true)}>계정 등록</button>
          <button onClick={() => setDirect(true)}>API 키 가져오기</button>
        </div>
      </div>
      {act.error && <p className="err">{act.error}</p>}
      <div className="table-wrap">
        <table>
          <thead><tr><th>이메일</th><th>프로바이더</th><th>소유자</th><th>배정 제외</th><th>구독료</th><th>유형</th><th>상태</th><th>잔여(5h/7d)</th><th>시크릿</th><th>만료</th><th></th></tr></thead>
          <tbody>
            {accounts.map((a) => (
              <tr key={a.id}>
                <td><EmailChip email={a.email} sub={a.organizationName} /></td>
                <td><ProviderTag value={a.provider} /></td>
                <td>{a.owner ? a.owner : <span className="muted">—</span>}</td>
                <td>
                  {a.assignmentExcluded ? (
                    <span className="badge disabled"><span className="dot" />제외</span>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                <td>
                  {a.monthlyPrice ? `${a.monthlyPrice} ${a.currency ?? 'USD'}` : <span className="muted">—</span>}
                </td>
                <td>{krLabel(a.credentialType)}</td>
                <td><Badge value={a.status} /></td>
                <td><UsageCell usage={a.usage} /></td>
                <td><code>{a.secretMasked}</code></td>
                <td><TimeCell iso={a.credentialExpiresAt} /></td>
                <td>
                  <div className="actions row-actions">
                    <button disabled={act.busy} onClick={() => setEditing(a)}>수정</button>
                    <button
                      className="danger"
                      disabled={act.busy}
                      onClick={() => act.run(() => api.deleteAccount(tenantId, a.id), () => mutate())}
                    >
                      삭제
                    </button>
                  </div>
                </td>
              </tr>
            ))}
            {accounts.length === 0 && (
              <tr><td colSpan={11} className="muted">등록된 계정이 없습니다. '계정 등록'으로 Claude·Codex 계정을 연결하세요.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      {wizard && <RegisterModal tenantId={tenantId} onClose={() => setWizard(false)} onDone={() => { setWizard(false); mutate(); }} />}
      {direct && <DirectImport tenantId={tenantId} onClose={() => setDirect(false)} onDone={() => { setDirect(false); mutate(); }} />}
      {editing && (
        <EditAccount
          tenantId={tenantId}
          account={editing}
          onClose={() => setEditing(null)}
          onDone={() => { setEditing(null); mutate(); }}
        />
      )}
    </div>
  );
}
