'use client';

// 이벤트 타임라인 — 기존 대시보드 홈의 ActivityFeed를 그대로 옮겨 왔다. 새 API
// 없이 할당·계정·경보 SWR(다른 탭과 같은 키라 폴링이 늘지 않는다)만 합성해서
// 최근 8건을 시간 역순으로 보여준다.
import type { ReactNode } from 'react';
import useSWR from 'swr';
import { api, krLastError } from '@/lib/api-client/client';
import { currentActiveByServer } from '@/components/AssignmentsPanel';
import { fmtClock, Icon, LiveDot, type IconName } from '@/components/common';
import type { AccountPage, AlertPage, AssignmentPage } from '@/lib/api-client/types';

type Activity = { id: string; at: number; icon: IconName; tone: string; text: ReactNode };

export function StatsTimeline({ tenantId }: { tenantId: string }) {
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
        text: <><span className="mono">{email}</span> 오류: {krLastError(a.lastError)}</>,
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
      text: <>알림 발생: {al.kind}</>,
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
