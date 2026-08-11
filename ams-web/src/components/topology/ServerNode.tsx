'use client';

import type { PointerEvent as ReactPointerEvent, Ref } from 'react';
import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import type { Server, UsageSnapshot } from '@/lib/api-client/types';
import { EmailChip, Icon, SwitchModePill } from '../common';

// 게이지 색 임계: 90%↑ crit, 70%↑ warn, 그 외 accent.
function gaugeTone(pct: number): '' | 'warn' | 'crit' {
  if (pct >= 90) return 'crit';
  if (pct >= 70) return 'warn';
  return '';
}

// 얇은 수평 게이지 한 줄. pct가 undefined면 같은 높이의 "메트릭 미보고" 폴백을
// 렌더해 카드 높이가 흔들리지 않게 한다.
function Gauge({ label, pct }: { label: string; pct?: number }) {
  if (pct === undefined || Number.isNaN(pct)) {
    return (
      <div className="gauge-row">
        <span className="gauge-label">{label}</span>
        <span className="gauge-missing">메트릭 미보고</span>
      </div>
    );
  }
  const v = Math.max(0, Math.min(100, pct));
  const tone = gaugeTone(v);
  return (
    <div className="gauge-row">
      <span className="gauge-label">{label}</span>
      <span className="gauge-track">
        <span className={`gauge-fill ${tone}`} style={{ width: `${v}%` }} />
      </span>
      <span className="gauge-val">{Math.round(v)}%</span>
    </div>
  );
}

// 서버 노드 카드 — 이름·상태 도트·전환 모드 알약 / CPU·MEM·DISK·토큰 게이지 /
// 활성 계정 칩. 토큰 게이지는 서버별 사용량 보고(getUsage)의 풀 최대 사용률을
// 사용한다. 이 SWR 키(['usage',t,id])는 사용량 모달과 동일하며 신규 API가 아니다.
export function ServerNode({
  tenantId,
  server,
  activeEmail,
  nodeRef,
  onClick,
  onPortDown,
}: {
  tenantId: string;
  server: Server;
  activeEmail?: string;
  nodeRef?: Ref<HTMLButtonElement>;
  onClick?: () => void;
  onPortDown?: (e: ReactPointerEvent) => void;
}) {
  // 오프라인 서버는 폴링하지 않고 토큰 게이지를 "미보고"로 둔다(부하 완화).
  const online = server.status === 'online';
  const { data: usage } = useSWR<UsageSnapshot>(
    online ? ['usage', tenantId, server.id] : null,
    () => api.getUsage(tenantId, server.id),
    { refreshInterval: 30000, shouldRetryOnError: false },
  );
  const tokenPct = usage?.payload?.poolSummary?.maxUtilizationPct;

  return (
    <button
      ref={nodeRef}
      type="button"
      className={`topo-node topo-srv ${server.status}`}
      data-node-type="server"
      data-node-id={server.id}
      onClick={onClick}
    >
      <span className="topo-port right" aria-label="연결점" onPointerDown={onPortDown} />
      <div className="topo-srv-head">
        <span className={`topo-dot ${server.status}`} aria-hidden="true" />
        <span className="topo-srv-name">{server.name}</span>
        <SwitchModePill mode={server.switchMode} />
      </div>
      <div className="topo-gauges">
        <Gauge label="CPU" pct={server.cpuPct} />
        <Gauge label="MEM" pct={server.memPct} />
        <Gauge label="DISK" pct={server.diskPct} />
        <Gauge label="토큰" pct={tokenPct} />
      </div>
      <div className="topo-srv-active">
        {activeEmail ? (
          <EmailChip email={activeEmail} sub="활성" />
        ) : (
          <span className="topo-srv-idle">
            <Icon name="pause" size={12} />
            활성 계정 없음
          </span>
        )}
      </div>
    </button>
  );
}
