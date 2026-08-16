'use client';

import type { PointerEvent as ReactPointerEvent, Ref } from 'react';
import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import type { Server, UsageSnapshot } from '@/lib/api-client/types';
import { EmailChip, Icon, krLabel, RackGlyph, SwitchModePill } from '../common';

// 게이지 색 임계: 90%↑ crit, 70%↑ warn, 그 외 accent.
function gaugeTone(pct: number): '' | 'warn' | 'crit' {
  if (pct >= 90) return 'crit';
  if (pct >= 70) return 'warn';
  return '';
}

// 얇은 수평 게이지 한 줄. pct가 없으면(undefined/null) 같은 높이의 "메트릭 미보고"
// 폴백을 렌더해 카드 높이가 흔들리지 않게 한다. null도 걸러야 0% 막대 오표시를
// 막는다(== null 은 undefined·null 모두 매칭).
function Gauge({ label, pct }: { label: string; pct?: number | null }) {
  if (pct == null || Number.isNaN(pct)) {
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

// 서버 노드 카드 — NMS 장비 은유. 랙 실루엣 아이콘 + 서버명 / 상태 텍스트·전환
// 모드 / 우상단 상태 LED(맥동) / 미니 게이지 2줄(CPU·풀 최대 소진율) / 활성 계정 칩.
// 상세 게이지 4종(CPU·MEM·DISK·토큰)은 호버 팝오버로 옮겼다. 토큰(소진) 게이지는
// 서버별 사용량 보고(getUsage)의 풀 최대 사용률을 쓴다. 이 SWR 키(['usage',t,id])는
// 사용량 모달과 동일하며 신규 API가 아니다.
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
      {/* 우상단 상태 LED — online 초록(맥동)·offline 붉은·degraded 호박. */}
      <span className={`topo-srv-led ${server.status}`} aria-hidden="true" />
      <div className="topo-srv-head">
        <span className="topo-srv-glyph"><RackGlyph size={26} /></span>
        <span className="topo-srv-name">{server.name}</span>
      </div>
      <div className="topo-srv-meta">
        <span className={`topo-srv-status ${server.status}`}>{krLabel(server.status)}</span>
        <SwitchModePill mode={server.switchMode} />
      </div>
      <div className="topo-gauges mini">
        <Gauge label="CPU" pct={server.cpuPct} />
        <Gauge label="풀" pct={tokenPct} />
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
      {/* 호버 상세 카드 — 게이지 4종 전체. 기존 팝오버 스타일 계열. */}
      <div className="topo-srv-detail" role="presentation">
        <div className="topo-srv-detail-head">호스트 텔레메트리</div>
        <div className="topo-gauges">
          <Gauge label="CPU" pct={server.cpuPct} />
          <Gauge label="MEM" pct={server.memPct} />
          <Gauge label="DISK" pct={server.diskPct} />
          <Gauge label="토큰" pct={tokenPct} />
        </div>
      </div>
    </button>
  );
}
