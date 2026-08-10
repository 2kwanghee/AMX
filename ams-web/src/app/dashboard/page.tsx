'use client';

import { useEffect, useState } from 'react';
import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import { readNavSession, type NavSession } from '@/lib/nav-session';
import type {
  AccountPage,
  AlertPage,
  AssignmentPage,
  ServerPage,
  TenantPage,
} from '@/lib/api-client/types';
import { AccountsPanel } from '@/components/AccountsPanel';
import { AlertsBadge, AlertsPanel } from '@/components/AlertsPanel';
import { AssignmentsPanel } from '@/components/AssignmentsPanel';
import { ServersPanel } from '@/components/ServersPanel';
import { Modal, useAction } from '@/components/common';

type Tab = 'home' | 'servers' | 'accounts' | 'assignments' | 'alerts';

const MENU: { id: Tab; label: string }[] = [
  { id: 'home', label: '대시보드' },
  { id: 'servers', label: '서버' },
  { id: 'accounts', label: '계정' },
  { id: 'assignments', label: '할당' },
  { id: 'alerts', label: '알림' },
];

const TITLES: Record<Tab, string> = {
  home: '대시보드',
  servers: '서버',
  accounts: '계정',
  assignments: '할당',
  alerts: '알림',
};

export default function Dashboard() {
  const { data, mutate } = useSWR<TenantPage>('tenants', () => api.listTenants());
  const tenants = data?.items ?? [];
  const [tenantId, setTenantId] = useState('');
  const [tab, setTab] = useState<Tab>('home');
  const [creatingTenant, setCreatingTenant] = useState(false);
  // Nav filter (UI convenience only; ams-server enforces scope). Read after
  // mount so the readable nav cookie is available client-side.
  const [nav, setNav] = useState<NavSession | null>(null);
  useEffect(() => setNav(readNavSession()), []);
  const isGlobalAdmin = nav?.role === 'global-admin';

  const active = tenantId || tenants[0]?.id || '';

  async function logout() {
    await fetch('/bff/session', { method: 'DELETE', credentials: 'same-origin' });
    window.location.href = '/login';
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-dot" />
          AMX 관제 콘솔
        </div>

        <div>
          <select
            value={active}
            onChange={(e) => setTenantId(e.target.value)}
            aria-label="테넌트 선택"
          >
            {tenants.length === 0 && <option value="">— 없음 —</option>}
            {tenants.map((t) => (
              <option key={t.id} value={t.id}>{t.name} ({t.status})</option>
            ))}
          </select>
          {isGlobalAdmin && (
            <button style={{ marginTop: 8, width: '100%' }} onClick={() => setCreatingTenant(true)}>
              새 테넌트
            </button>
          )}
        </div>

        <nav className="nav">
          {MENU.map((m) => (
            <button
              key={m.id}
              className={`nav-item ${tab === m.id ? 'active' : ''}`}
              onClick={() => setTab(m.id)}
            >
              {m.label}
              {m.id === 'alerts' && active && (
                <span className="nav-count"><AlertsBadge tenantId={active} /></span>
              )}
            </button>
          ))}
        </nav>

        <div className="sidebar-foot">
          <div className="muted" style={{ fontSize: 12, padding: '0 4px' }}>
            {nav?.role === 'global-admin' ? '전체 관리자' : nav?.role ? '테넌트 관리자' : '관리자'}
          </div>
          <button onClick={logout}>로그아웃</button>
        </div>
      </aside>

      <main className="main">
        <h1>{TITLES[tab]}</h1>
        {!active && <p className="muted">테넌트가 없습니다. 새 테넌트를 만들어 시작하세요.</p>}

        {active && tab === 'home' && (
          <>
            <KpiStrip tenantId={active} onGo={setTab} />
            <ServersPanel tenantId={active} />
            <AssignmentsPanel tenantId={active} />
          </>
        )}
        {active && tab === 'servers' && <ServersPanel tenantId={active} />}
        {active && tab === 'accounts' && <AccountsPanel tenantId={active} />}
        {active && tab === 'assignments' && <AssignmentsPanel tenantId={active} />}
        {active && tab === 'alerts' && <AlertsPanel tenantId={active} />}
      </main>

      {creatingTenant && (
        <CreateTenant onClose={() => setCreatingTenant(false)} onDone={() => { setCreatingTenant(false); mutate(); }} />
      )}
    </div>
  );
}

// KPI 스트립 — 각 패널과 동일한 SWR 키를 재사용해 폴링을 중복시키지 않는다.
function KpiStrip({ tenantId, onGo }: { tenantId: string; onGo: (t: Tab) => void }) {
  const { data: servers } = useSWR<ServerPage>(['servers', tenantId], () => api.listServers(tenantId));
  const { data: accounts } = useSWR<AccountPage>(['accounts', tenantId], () => api.listAccounts(tenantId));
  const { data: assignments } = useSWR<AssignmentPage>(['assignments', tenantId], () => api.listAssignments(tenantId));
  const { data: alerts } = useSWR<AlertPage>(['alerts', tenantId, 'open'], () => api.listAlerts(tenantId, 'open'));

  const serverItems = servers?.items ?? [];
  const onlineCount = serverItems.filter((s) => s.status === 'online').length;
  const activeAssignments = (assignments?.items ?? []).filter((a) => a.state === 'active').length;

  return (
    <div className="kpi-grid">
      <button className="kpi" onClick={() => onGo('servers')}>
        <div className="kpi-label">온라인 서버</div>
        <div className="kpi-value">{onlineCount}<small> / {serverItems.length}</small></div>
      </button>
      <button className="kpi" onClick={() => onGo('accounts')}>
        <div className="kpi-label">등록 계정</div>
        <div className="kpi-value">{accounts?.items?.length ?? 0}</div>
      </button>
      <button className="kpi" onClick={() => onGo('assignments')}>
        <div className="kpi-label">활성 할당</div>
        <div className="kpi-value">{activeAssignments}</div>
      </button>
      <button className="kpi" onClick={() => onGo('alerts')}>
        <div className="kpi-label">미확인 알림</div>
        <div className="kpi-value">{alerts?.items?.length ?? 0}</div>
      </button>
    </div>
  );
}

function CreateTenant({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState('');
  const act = useAction();
  return (
    <Modal title="새 테넌트" onClose={onClose}>
      <label>이름</label>
      <input value={name} onChange={(e) => setName(e.target.value)} autoFocus />
      {act.error && <p className="err">{act.error}</p>}
      <button
        className="primary"
        style={{ marginTop: 14 }}
        disabled={act.busy || !name}
        onClick={() => act.run(() => api.createTenant({ name }), onDone)}
      >
        만들기
      </button>
    </Modal>
  );
}
