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
import { Modal, useAction } from '@/components/common';

type Tab = 'overview' | 'alerts';

export default function Dashboard() {
  const { data, mutate } = useSWR<TenantPage>('tenants', () => api.listTenants());
  const tenants = data?.items ?? [];
  const [tenantId, setTenantId] = useState('');
  const [tab, setTab] = useState<Tab>('overview');
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
    <>
      <div className="topbar">
        <b>AMX Console</b>
        <label style={{ margin: 0 }}>Tenant</label>
        <select
          style={{ width: 260 }}
          value={active}
          onChange={(e) => setTenantId(e.target.value)}
        >
          {tenants.length === 0 && <option value="">— none —</option>}
          {tenants.map((t) => <option key={t.id} value={t.id}>{t.name} ({t.status})</option>)}
        </select>
        {isGlobalAdmin && <button onClick={() => setCreatingTenant(true)}>+ Tenant</button>}
        <div style={{ flex: 1 }} />
        {active && (
          <span className="tabs" style={{ margin: 0 }}>
            <button className={tab === 'overview' ? 'active' : ''} onClick={() => setTab('overview')}>
              Overview
            </button>
            <button className={tab === 'alerts' ? 'active' : ''} onClick={() => setTab('alerts')}>
              Alerts <AlertsBadge tenantId={active} />
            </button>
          </span>
        )}
        <button onClick={logout}>Logout</button>
      </div>

      <div className="container">
        {!active && <p className="muted">No tenant yet. Create one to begin.</p>}
        {active && tab === 'overview' && (
          <>
            <ServersPanel tenantId={active} />
            <AccountsPanel tenantId={active} />
            <AssignmentsPanel tenantId={active} />
          </>
        )}
        {active && tab === 'alerts' && <AlertsPanel tenantId={active} />}
      </div>

      {creatingTenant && (
        <CreateTenant onClose={() => setCreatingTenant(false)} onDone={() => { setCreatingTenant(false); mutate(); }} />
      )}
    </>
  );
}

function CreateTenant({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState('');
  const act = useAction();
  return (
    <Modal title="Create tenant" onClose={onClose}>
      <label>Name</label>
      <input value={name} onChange={(e) => setName(e.target.value)} autoFocus />
      {act.error && <p className="err">{act.error}</p>}
      <button
        className="primary"
        style={{ marginTop: 14 }}
        disabled={act.busy || !name}
        onClick={() => act.run(() => api.createTenant({ name }), onDone)}
      >
        Create
      </button>
    </Modal>
  );
}
