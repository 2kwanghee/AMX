'use client';

import type { ReactNode } from 'react';
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
import { AssignmentsPanel, currentActiveByServer } from '@/components/AssignmentsPanel';
import { ServersPanel } from '@/components/ServersPanel';
import { SetupGuidePanel } from '@/components/SetupGuidePanel';
import { UsageCostPanel } from '@/components/UsageCostPanel';
import { LangfuseUsagePanel } from '@/components/LangfuseUsagePanel';
import { TopologyView } from '@/components/topology/TopologyView';
import {
  ConsoleHeader,
  Icon,
  LiveDot,
  MeshBackdrop,
  Modal,
  Sparkline,
  fmtClock,
  markDataArrived,
  useAction,
  useSeries,
  type IconName,
} from '@/components/common';

type Tab =
  | 'home'
  | 'console'
  | 'servers'
  | 'accounts'
  | 'assignments'
  | 'alerts'
  | 'usage'
  | 'guide';

const MENU: { id: Tab; label: string; icon: IconName }[] = [
  { id: 'home', label: '대시보드', icon: 'grid' },
  { id: 'console', label: '상황판', icon: 'zap' },
  { id: 'servers', label: '서버', icon: 'server' },
  { id: 'accounts', label: '계정', icon: 'user' },
  { id: 'assignments', label: '할당', icon: 'link' },
  { id: 'alerts', label: '알림', icon: 'bell' },
  { id: 'usage', label: '사용량', icon: 'gauge' },
  { id: 'guide', label: '설치 가이드', icon: 'help' },
];

const TITLES: Record<Tab, string> = {
  home: '대시보드',
  console: '상황판',
  servers: '서버',
  accounts: '계정',
  assignments: '할당',
  alerts: '알림',
  usage: '사용량',
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
          <>
            <KpiStrip tenantId={active} onGo={setTab} />
            <ServersPanel tenantId={active} variant="home" />
            <AssignmentsPanel tenantId={active} />
            <ActivityFeed tenantId={active} />
          </>
        )}
        {active && tab === 'console' && <TopologyView tenantId={active} onGo={setTab} />}
        {active && tab === 'servers' && <ServersPanel tenantId={active} />}
        {active && tab === 'accounts' && <AccountsPanel tenantId={active} />}
        {active && tab === 'assignments' && <AssignmentsPanel tenantId={active} />}
        {active && tab === 'alerts' && <AlertsPanel tenantId={active} />}
        {active && tab === 'usage' && (
          <>
            <UsageCostPanel tenantId={active} onGoAccounts={() => setTab('accounts')} />
            <LangfuseUsagePanel tenantId={active} />
          </>
        )}
        {tab === 'guide' && <SetupGuidePanel />}
      </main>

      {creatingTenant && (
        <CreateTenant onClose={() => setCreatingTenant(false)} onDone={() => { setCreatingTenant(false); mutate(); }} />
      )}
    </div>
  );
}

// KPI 스트립 — 각 패널과 동일한 SWR 키를 재사용해 폴링을 중복시키지 않는다.
// onSuccess로 폴링마다 값을 클라이언트 메모리(useSeries)에 축적해 스파크라인을
// 그린다(값 변동 없으면 평평한 선 = 라이브 수집 질감). markDataArrived로 헤더의
// "갱신 시각"도 갱신한다. SWR 키·폴링 주기는 변경하지 않는다.
function KpiStrip({ tenantId, onGo }: { tenantId: string; onGo: (t: Tab) => void }) {
  const onlineSeries = useSeries();
  const accountSeries = useSeries();
  const assignSeries = useSeries();
  const alertSeries = useSeries();

  const { data: servers } = useSWR<ServerPage>(['servers', tenantId], () => api.listServers(tenantId), {
    onSuccess: (d) => {
      onlineSeries.push((d.items ?? []).filter((s) => s.status === 'online').length);
      markDataArrived();
    },
  });
  const { data: accounts } = useSWR<AccountPage>(['accounts', tenantId], () => api.listAccounts(tenantId), {
    onSuccess: (d) => { accountSeries.push(d.items?.length ?? 0); markDataArrived(); },
  });
  const { data: assignments } = useSWR<AssignmentPage>(['assignments', tenantId], () => api.listAssignments(tenantId), {
    onSuccess: (d) => { assignSeries.push((d.items ?? []).filter((a) => a.state === 'active').length); markDataArrived(); },
  });
  const { data: alerts } = useSWR<AlertPage>(['alerts', tenantId, 'open'], () => api.listAlerts(tenantId, 'open'), {
    onSuccess: (d) => { alertSeries.push(d.items?.length ?? 0); markDataArrived(); },
  });

  const serverItems = servers?.items ?? [];
  const onlineCount = serverItems.filter((s) => s.status === 'online').length;
  const hasOffline = serverItems.some((s) => s.status === 'offline');
  const activeAssignments = (assignments?.items ?? []).filter((a) => a.state === 'active').length;

  return (
    <div className="kpi-grid">
      <button className={`kpi kpi-teal ${hasOffline ? 'warn-edge' : ''}`} onClick={() => onGo('servers')}>
        <span className="kpi-chip"><Icon name="server" size={20} /></span>
        <span className="kpi-body">
          <span className="kpi-label">온라인 서버</span>
          <div className="kpi-value">{onlineCount}<small> / {serverItems.length}</small></div>
          <div className="kpi-spark"><Sparkline data={onlineSeries.data} /></div>
        </span>
      </button>
      <button className="kpi kpi-indigo" onClick={() => onGo('accounts')}>
        <span className="kpi-chip"><Icon name="user" size={20} /></span>
        <span className="kpi-body">
          <span className="kpi-label">등록 계정</span>
          <div className="kpi-value">{accounts?.items?.length ?? 0}</div>
          <div className="kpi-spark"><Sparkline data={accountSeries.data} /></div>
        </span>
      </button>
      <button className="kpi kpi-amber" onClick={() => onGo('assignments')}>
        <span className="kpi-chip"><Icon name="link" size={20} /></span>
        <span className="kpi-body">
          <span className="kpi-label">활성 할당</span>
          <div className="kpi-value">{activeAssignments}</div>
          <div className="kpi-spark"><Sparkline data={assignSeries.data} /></div>
        </span>
      </button>
      <button className="kpi kpi-rose" onClick={() => onGo('alerts')}>
        <span className="kpi-chip"><Icon name="bell" size={20} /></span>
        <span className="kpi-body">
          <span className="kpi-label">미확인 알림</span>
          <div className="kpi-value">{alerts?.items?.length ?? 0}</div>
          <div className="kpi-spark"><Sparkline data={alertSeries.data} /></div>
        </span>
      </button>
    </div>
  );
}

// 최근 활동 피드 — 새 API 없이 기존 SWR 데이터(할당·알림·계정)만 합성한다.
// 같은 SWR 키를 재사용하므로 폴링을 늘리지 않는다. 최신순 최대 8개.
type Activity = { id: string; at: number; icon: IconName; tone: string; text: ReactNode };

function ActivityFeed({ tenantId }: { tenantId: string }) {
  const { data: assignments } = useSWR<AssignmentPage>(['assignments', tenantId], () => api.listAssignments(tenantId));
  const { data: accounts } = useSWR<AccountPage>(['accounts', tenantId], () => api.listAccounts(tenantId));
  const { data: alerts } = useSWR<AlertPage>(['alerts', tenantId, 'open'], () => api.listAlerts(tenantId, 'open'));

  const accItems = accounts?.items ?? [];
  const asgItems = assignments?.items ?? [];
  const emailOf = new Map(accItems.map((a) => [a.id, a.email]));
  const serverOfAccount = new Map(asgItems.map((a) => [a.accountId, a.serverId]));
  const activeByServer = currentActiveByServer(asgItems, accItems);

  const events: Activity[] = [];
  const ms = (iso?: string) => (iso ? new Date(iso).getTime() : NaN);

  for (const a of asgItems) {
    const email = emailOf.get(a.accountId) ?? a.accountId.slice(0, 8);
    if (a.lastError) {
      const t = ms(a.ackedAt) || ms(a.deliveredAt);
      events.push({
        id: `err-${a.id}`, at: Number.isNaN(t) ? Date.now() : t, icon: 'alert', tone: 'crit',
        text: <><span className="mono">{email}</span> 오류: {a.lastError}</>,
      });
    } else if (a.ackedAt) {
      events.push({
        id: `ack-${a.id}`, at: ms(a.ackedAt), icon: 'check', tone: 'ok',
        text: <><span className="mono">{email}</span> 활성 확인</>,
      });
    } else if (a.deliveredAt) {
      events.push({
        id: `dlv-${a.id}`, at: ms(a.deliveredAt), icon: 'send', tone: 'accent',
        text: <><span className="mono">{email}</span> 전달 완료</>,
      });
    }
  }
  for (const acc of accItems) {
    if (!acc.lastSwitchedAt) continue;
    const t = ms(acc.lastSwitchedAt);
    if (Number.isNaN(t)) continue;
    // 전환 이벤트는 현재 활성 계정에 한해 "→ 서버명"으로 표기(가능한 경우).
    const sid = serverOfAccount.get(acc.id);
    const isActive = sid && activeByServer.get(sid) === acc.id;
    events.push({
      id: `sw-${acc.id}`, at: t, icon: 'zap', tone: isActive ? 'accent' : '',
      text: <><span className="mono">{acc.email}</span> 계정 전환</>,
    });
  }
  for (const al of alerts?.items ?? []) {
    events.push({
      id: `al-${al.id}`, at: ms(al.createdAt), icon: 'bell', tone: al.severity === 'critical' ? 'crit' : 'warn',
      text: <>알림 발생 — {al.kind}</>,
    });
  }

  const sorted = events
    .filter((e) => !Number.isNaN(e.at))
    .sort((a, b) => b.at - a.at)
    .slice(0, 8);

  return (
    <div className="panel">
      <h2>최근 활동<LiveDot /></h2>
      {sorted.length === 0 && <p className="muted">최근 활동이 없습니다.</p>}
      {sorted.length > 0 && (
        <div className="activity-list">
          {sorted.map((e) => (
            <div className="activity-item" key={e.id}>
              <span className="activity-time">{fmtClock(e.at)}</span>
              <span className={`activity-icon ${e.tone}`}><Icon name={e.icon} size={15} /></span>
              <span className="activity-text">{e.text}</span>
            </div>
          ))}
        </div>
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
