'use client';

import { fmtClock, Icon, useLastDataAt, useNow, type IconName } from '../common';

export type StatKey = 'servers' | 'assignments' | 'accounts' | 'alerts';

type Stat = {
  key: StatKey;
  icon: IconName;
  label: string;
  value: string;
  tone?: 'warn' | 'crit';
};

// 상단 스탯 바 — 온라인 서버 n/m · 활성 할당 n · 최근 1시간 전환 k · 미확인 알림 n.
// 각 스탯 클릭 시 해당 표 탭으로 이동한다. 우측에 LIVE 펄스 + 마지막 갱신 시각
// (ConsoleHeader 자산 재사용: useLastDataAt/fmtClock).
export function StatBar({
  onlineServers,
  totalServers,
  activeAssignments,
  switchesLastHour,
  openAlerts,
  onGo,
}: {
  onlineServers: number;
  totalServers: number;
  activeAssignments: number;
  switchesLastHour: number;
  openAlerts: number;
  onGo: (k: StatKey) => void;
}) {
  const now = useNow(1000);
  const updatedAt = useLastDataAt();

  const stats: Stat[] = [
    {
      key: 'servers',
      icon: 'server',
      label: '온라인 서버',
      value: `${onlineServers} / ${totalServers}`,
      tone: onlineServers < totalServers ? 'warn' : undefined,
    },
    { key: 'assignments', icon: 'link', label: '활성 할당', value: String(activeAssignments) },
    { key: 'accounts', icon: 'zap', label: '최근 1시간 전환', value: String(switchesLastHour) },
    {
      key: 'alerts',
      icon: 'bell',
      label: '미확인 알림',
      value: String(openAlerts),
      tone: openAlerts > 0 ? 'crit' : undefined,
    },
  ];

  return (
    <div className="topo-statbar">
      <div className="topo-stats">
        {stats.map((s) => (
          <button key={s.key} className={`topo-stat ${s.tone ?? ''}`.trim()} onClick={() => onGo(s.key)}>
            <span className="topo-stat-icon"><Icon name={s.icon} size={16} /></span>
            <span className="topo-stat-body">
              <span className="topo-stat-label">{s.label}</span>
              <span className="topo-stat-value">{s.value}</span>
            </span>
          </button>
        ))}
      </div>
      <div className="topo-live" aria-live="off">
        <span className="live-pill"><span className="live-pip" />LIVE</span>
        <span className="topo-live-updated">갱신 <span className="mono">{updatedAt ? fmtClock(updatedAt) : '--:--:--'}</span></span>
        <span className="topo-live-clock mono"><Icon name="clock" size={13} />{fmtClock(now)}</span>
      </div>
    </div>
  );
}
