'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import type {
  AccountPage,
  AssignmentPage,
  EnrollTokenResponse,
  Server,
  ServerPage,
} from '@/lib/api-client/types';
import { currentActiveByServer } from './AssignmentsPanel';
import { CreateServer } from './servers/CreateServerModal';
import { POLL } from './servers/constants';
import { EditServer } from './servers/EditServerModal';
import { EnrollTokenModal } from './servers/EnrollTokenModal';
import { EventsModal } from './servers/EventsModal';
import { PolicyModal } from './servers/PolicyModal';
import { SelfUpdateModal } from './servers/SelfUpdateModal';
import { UsageModal } from './servers/UsageModal';
import {
  AvatarStack,
  Badge,
  Icon,
  LiveDot,
  ownerSuggestions,
  SwitchModePill,
  TimeCell,
  useAction,
  useMarkOnData,
  useNow,
} from './common';

// 하트비트 메트릭 표기 — 에이전트가 아직 보고 안 했으면(구버전 포함) null이다.
function fmtPct(n?: number | null) {
  return n === undefined || n === null ? '—' : `${Math.round(n)}%`;
}

// 마지막 접속 카운트업 표기. offline이면 crit 톤, 90초 초과면 warn + 경고 아이콘.
function lastSeen(iso: string | undefined, offline: boolean, now: number) {
  if (!iso) return { cls: offline ? 'crit' : '', warn: offline, text: '접속 기록 없음' };
  const secs = Math.max(0, Math.floor((now - new Date(iso).getTime()) / 1000));
  const dur = secs < 60 ? `${secs}초` : `${Math.floor(secs / 60)}분 ${secs % 60}초`;
  if (offline) return { cls: 'crit', warn: true, text: `오프라인 · 마지막 ${dur} 전` };
  if (secs > 90) return { cls: 'warn', warn: true, text: `마지막 접속 ${dur} 전` };
  return { cls: '', warn: false, text: `마지막 접속 ${secs}초 전` };
}

export function ServersPanel({ tenantId, variant = 'full' }: { tenantId: string; variant?: 'home' | 'full' }) {
  const { data, mutate } = useSWR<ServerPage>(
    ['servers', tenantId],
    () => api.listServers(tenantId),
    { refreshInterval: POLL },
  );
  // 아바타 스택·현재 활성 판정을 위해 계정·할당 목록을 같은 SWR 키로 재사용한다.
  const { data: accountsData } = useSWR<AccountPage>(['accounts', tenantId], () => api.listAccounts(tenantId));
  const { data: assignData } = useSWR<AssignmentPage>(['assignments', tenantId], () => api.listAssignments(tenantId));
  useMarkOnData(data);
  const [creating, setCreating] = useState(false);
  const [usageOf, setUsageOf] = useState<Server | null>(null);
  const [policyOf, setPolicyOf] = useState<Server | null>(null);
  const [eventsOf, setEventsOf] = useState<Server | null>(null);
  const [tokenOf, setTokenOf] = useState<EnrollTokenResponse | null>(null);
  const [updateOf, setUpdateOf] = useState<Server | null>(null);
  const [editingOf, setEditingOf] = useState<Server | null>(null);
  const act = useAction();
  const now = useNow(1000);
  const servers = data?.items ?? [];

  const accItems = accountsData?.items ?? [];
  const ownerOptions = ownerSuggestions(servers, accItems);
  const asgItems = assignData?.items ?? [];
  const emailOf = new Map(accItems.map((a) => [a.id, a.email]));
  const activeByServer = currentActiveByServer(asgItems, accItems);
  const accountsByServer = new Map<string, string[]>();
  for (const a of asgItems) {
    if (a.state === 'detached') continue; // 회수된 연결은 서버 타일 아바타에서 제외
    const email = emailOf.get(a.accountId);
    if (!email) continue;
    const arr = accountsByServer.get(a.serverId) ?? [];
    arr.push(email);
    accountsByServer.set(a.serverId, arr);
  }

  function toggleMode(s: Server) {
    act.run(
      () => api.setSwitchMode(tenantId, s.id, s.switchMode === 'auto' ? 'manual' : 'auto'),
      () => mutate(),
    );
  }

  const tiles = (
    <div className="srv-tiles">
      {servers.map((s) => {
        const offline = s.status === 'offline';
        const ls = lastSeen(s.lastSeenAt, offline, now);
        const emails = accountsByServer.get(s.id) ?? [];
        const activeId = activeByServer.get(s.id);
        const activeEmail = activeId ? emailOf.get(activeId) : undefined;
        return (
          <div key={s.id} className={`srv-tile ${offline ? 'offline' : ''}`}>
            <div className="srv-tile-head">
              <div style={{ minWidth: 0 }}>
                <div className="srv-tile-name">{s.name}</div>
                {s.hostname && <div className="srv-tile-host">{s.hostname}</div>}
                <div className="muted" style={{ fontSize: 12 }}>
                  소유자 {s.owner || '—'}
                </div>
              </div>
              <div className="srv-tile-status">
                <span className={`srv-dot ${s.status}`} aria-hidden="true" />
                <Badge value={s.status} />
              </div>
            </div>
            <div className="srv-tile-meta">
              <span className={`srv-last ${ls.cls}`}>
                {ls.warn ? <Icon name="alert" size={13} /> : <Icon name="clock" size={13} />}
                <span className="mono">{ls.text}</span>
              </span>
              <SwitchModePill mode={s.switchMode} />
            </div>
            <div className="muted mono" style={{ fontSize: 12 }}>
              CPU {fmtPct(s.cpuPct)} · MEM {fmtPct(s.memPct)} · DISK {fmtPct(s.diskPct)}
            </div>
            <div className="muted mono" style={{ fontSize: 12 }}>
              시트 엔진 {s.tsamxVersion || '미보고'}
            </div>
            <div className="srv-tile-accounts">
              {emails.length > 0 ? <AvatarStack emails={emails} /> : <span className="muted" style={{ fontSize: 12 }}>할당 계정 없음</span>}
              {activeEmail && <span className="srv-tile-current">활성 <b>{activeEmail}</b></span>}
            </div>
            <div className="srv-tile-actions">
              <button className="tile-btn" title="수정" onClick={() => setEditingOf(s)}>
                <Icon name="edit" size={15} />
              </button>
              <button className="tile-btn" title={s.switchMode === 'auto' ? '수동 전환으로' : '자동 전환으로'} disabled={act.busy} onClick={() => toggleMode(s)}>
                <Icon name={s.switchMode === 'auto' ? 'hand' : 'zap'} size={15} />
              </button>
              <button className="tile-btn" title="사용량 갱신" disabled={act.busy} onClick={() => act.run(() => api.refreshUsage(tenantId, s.id))}>
                <Icon name="refresh" size={15} />
              </button>
              <button className="tile-btn" title="사용량" onClick={() => setUsageOf(s)}><Icon name="gauge" size={15} /></button>
              <button className="tile-btn" title="이벤트" onClick={() => setEventsOf(s)}><Icon name="activity" size={15} /></button>
              <button className="tile-btn" title="정책" onClick={() => setPolicyOf(s)}><Icon name="sliders" size={15} /></button>
              <button
                className="tile-btn"
                title="등록 토큰"
                disabled={act.busy}
                onClick={() => act.run(async () => { const t = await api.issueEnrollToken(tenantId, s.id); setTokenOf(t); })}
              >
                <Icon name="key" size={15} />
              </button>
              <button className="tile-btn" title="에이전트 업데이트" disabled={act.busy} onClick={() => setUpdateOf(s)}>
                <Icon name="rotate" size={15} />
              </button>
              <button className="tile-btn danger" title="삭제" disabled={act.busy} onClick={() => act.run(() => api.deleteServer(tenantId, s.id), () => mutate())}>
                <Icon name="trash" size={15} />
              </button>
            </div>
          </div>
        );
      })}
      {servers.length === 0 && <div className="tile-empty">등록된 서버가 없습니다. &apos;서버 등록&apos;으로 시작하세요.</div>}
    </div>
  );

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>서버<LiveDot /></h2>
        <button className="primary" onClick={() => setCreating(true)}>서버 등록</button>
      </div>
      {act.error && <p className="err">{act.error}</p>}

      {tiles}

      {variant === 'full' && (
      <div className="table-wrap" style={{ marginTop: 16 }}>
        <table>
          <thead>
            <tr>
              <th>이름</th><th>소유자</th><th>상태</th><th>전환 모드</th><th className="num">할당 계정</th>
              <th className="num">CPU</th><th className="num">MEM</th><th className="num">DISK</th>
              <th>마지막 접속</th><th>동작</th>
            </tr>
          </thead>
          <tbody>
            {servers.map((s) => (
              <tr key={s.id}>
                <td>{s.name}<div className="muted">{s.hostname}</div></td>
                <td>{s.owner ? s.owner : <span className="muted">—</span>}</td>
                <td><Badge value={s.status} /></td>
                <td><SwitchModePill mode={s.switchMode} /></td>
                <td className="num">{s.assignedAccountCount ?? 0}</td>
                <td className="num mono">{fmtPct(s.cpuPct)}</td>
                <td className="num mono">{fmtPct(s.memPct)}</td>
                <td className="num mono">{fmtPct(s.diskPct)}</td>
                <td><TimeCell iso={s.lastSeenAt} /></td>
                <td>
                  <div className="actions row-actions">
                    <button className="tile-btn" title="수정" onClick={() => setEditingOf(s)}>
                      <Icon name="edit" size={15} />
                    </button>
                    <button
                      className="tile-btn"
                      title={s.switchMode === 'auto' ? '수동 전환으로' : '자동 전환으로'}
                      disabled={act.busy}
                      onClick={() =>
                        act.run(
                          () => api.setSwitchMode(tenantId, s.id, s.switchMode === 'auto' ? 'manual' : 'auto'),
                          () => mutate(),
                        )
                      }
                    >
                      <Icon name={s.switchMode === 'auto' ? 'hand' : 'zap'} size={15} />
                    </button>
                    <button
                      className="tile-btn"
                      title="사용량 갱신"
                      disabled={act.busy}
                      onClick={() => act.run(() => api.refreshUsage(tenantId, s.id))}
                    >
                      <Icon name="refresh" size={15} />
                    </button>
                    <button className="tile-btn" title="사용량" onClick={() => setUsageOf(s)}>
                      <Icon name="gauge" size={15} />
                    </button>
                    <button className="tile-btn" title="이벤트" onClick={() => setEventsOf(s)}>
                      <Icon name="activity" size={15} />
                    </button>
                    <button className="tile-btn" title="정책" onClick={() => setPolicyOf(s)}>
                      <Icon name="sliders" size={15} />
                    </button>
                    <button
                      className="tile-btn"
                      title="등록 토큰"
                      disabled={act.busy}
                      onClick={() =>
                        act.run(async () => {
                          const t = await api.issueEnrollToken(tenantId, s.id);
                          setTokenOf(t);
                        })
                      }
                    >
                      <Icon name="key" size={15} />
                    </button>
                    <button className="tile-btn" title="에이전트 업데이트" disabled={act.busy} onClick={() => setUpdateOf(s)}>
                      <Icon name="rotate" size={15} />
                    </button>
                    <button
                      className="tile-btn danger"
                      title="삭제"
                      disabled={act.busy}
                      onClick={() => act.run(() => api.deleteServer(tenantId, s.id), () => mutate())}
                    >
                      <Icon name="trash" size={15} />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
            {servers.length === 0 && (
              <tr><td colSpan={10} className="muted">등록된 서버가 없습니다. '서버 등록'으로 시작하세요.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      )}

      {creating && (
        <CreateServer
          tenantId={tenantId}
          ownerSuggestions={ownerOptions}
          onClose={() => setCreating(false)}
          onDone={() => { setCreating(false); mutate(); }}
        />
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
      {updateOf && (
        <SelfUpdateModal
          tenantId={tenantId}
          server={servers.find((x) => x.id === updateOf.id) ?? updateOf}
          onClose={() => setUpdateOf(null)}
        />
      )}
      {tokenOf && <EnrollTokenModal token={tokenOf} onClose={() => setTokenOf(null)} />}
      {editingOf && (
        <EditServer
          tenantId={tenantId}
          server={editingOf}
          ownerSuggestions={ownerOptions}
          onClose={() => setEditingOf(null)}
          onDone={() => { setEditingOf(null); mutate(); }}
        />
      )}
    </div>
  );
}
