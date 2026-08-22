'use client';

import { useEffect, useState } from 'react';
import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import { readNavSession, type NavSession } from '@/lib/nav-session';
import type { TenantPage } from '@/lib/api-client/types';
import { AccountsPanel } from '@/components/AccountsPanel';
import { AlertsBadge, AlertsPanel } from '@/components/AlertsPanel';
import { AssignmentsPanel } from '@/components/AssignmentsPanel';
import { ServersPanel } from '@/components/ServersPanel';
import { PoolPanel } from '@/components/PoolPanel';
import { SetupGuidePanel } from '@/components/SetupGuidePanel';
import { UsageCostPanel } from '@/components/UsageCostPanel';
import { LangfuseUsagePanel } from '@/components/LangfuseUsagePanel';
import { SessionUsagePanel } from '@/components/SessionUsagePanel';
import { AuditLogPanel } from '@/components/AuditLogPanel';
import { TopologyView } from '@/components/topology/TopologyView';
import { DashboardHome } from '@/components/dashboard/DashboardHome';
import {
  ConsoleHeader,
  Icon,
  MeshBackdrop,
  Modal,
  useAction,
  type IconName,
} from '@/components/common';

type Tab =
  | 'home'
  | 'console'
  | 'servers'
  | 'accounts'
  | 'assignments'
  | 'pool'
  | 'alerts'
  | 'usage'
  | 'audit'
  | 'guide';

const MENU: { id: Tab; label: string; icon: IconName }[] = [
  { id: 'home', label: '대시보드', icon: 'grid' },
  { id: 'console', label: '상황판', icon: 'zap' },
  { id: 'servers', label: '서버', icon: 'server' },
  { id: 'accounts', label: '계정', icon: 'user' },
  { id: 'assignments', label: '할당', icon: 'link' },
  { id: 'pool', label: '계정 풀', icon: 'rotate' },
  { id: 'alerts', label: '알림', icon: 'bell' },
  { id: 'usage', label: '사용량', icon: 'gauge' },
  { id: 'audit', label: '감사', icon: 'activity' },
  { id: 'guide', label: '설치 가이드', icon: 'help' },
];

const TITLES: Record<Tab, string> = {
  home: '대시보드',
  console: '상황판',
  servers: '서버',
  accounts: '계정',
  assignments: '할당',
  pool: '계정 풀',
  alerts: '알림',
  usage: '사용량',
  audit: '감사 로그',
  guide: '설치·운영 가이드',
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
      <MeshBackdrop variant="dashboard" />
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
              <span className="nav-icon"><Icon name={m.icon} size={17} /></span>
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
        <ConsoleHeader title={TITLES[tab]} />
        {!active && tab !== 'guide' && (
          <p className="muted">테넌트가 없습니다. 새 테넌트를 만들어 시작하세요.</p>
        )}

        {active && tab === 'home' && (
          <DashboardHome tenantId={active} onGo={(t) => setTab(t as Tab)} />
        )}
        {active && tab === 'console' && <TopologyView tenantId={active} />}
        {active && tab === 'servers' && <ServersPanel tenantId={active} />}
        {active && tab === 'accounts' && <AccountsPanel tenantId={active} />}
        {active && tab === 'assignments' && <AssignmentsPanel tenantId={active} />}
        {active && tab === 'pool' && <PoolPanel tenantId={active} />}
        {active && tab === 'alerts' && <AlertsPanel tenantId={active} />}
        {active && tab === 'usage' && (
          <>
            <UsageCostPanel tenantId={active} onGoAccounts={() => setTab('accounts')} />
            <LangfuseUsagePanel tenantId={active} />
            <SessionUsagePanel tenantId={active} />
          </>
        )}
        {active && tab === 'audit' && <AuditLogPanel key={active} tenantId={active} />}
        {tab === 'guide' && <SetupGuidePanel />}
      </main>

      {creatingTenant && (
        <CreateTenant onClose={() => setCreatingTenant(false)} onDone={() => { setCreatingTenant(false); mutate(); }} />
      )}
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
