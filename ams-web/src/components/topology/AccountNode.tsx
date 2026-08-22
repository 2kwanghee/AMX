'use client';

import type { PointerEvent as ReactPointerEvent, Ref } from 'react';
import type { AccountUsageSummary } from '@/lib/api-client/types';
import { fmtRemainingWindow } from '@/lib/usage-format';
import { krLabel, relTime, RobotAvatar } from '../common';

// 계정 노드 — 에이전트 캡슐. 좌측 연결점(port)에서 드래그해 서버에 연결한다. 노드
// 본문 클릭은 계정 탭 진입. data-node-* 는 드롭 히트테스트에 쓰인다.
//
// 상태 스타일(status-*): assigned=활성(실선+우상단 맥동 점), available=대기(점선),
// quarantined/disabled=붉은 테두리+회색조. usage(계정 API 1단계 필드)는 값이
// 있을 때만 라벨 끝에 "5h 잔여 n%"를 붙이고, 7일 창·리셋 시각·신선도는 호버
// 상세 카드로 옮긴다(.topo-srv-detail 패턴).
export function AccountNode({
  id,
  email,
  status,
  provider,
  usage,
  nodeRef,
  onClick,
  onPortDown,
}: {
  id: string;
  email: string;
  status?: string;
  provider?: string;
  usage?: AccountUsageSummary;
  nodeRef?: Ref<HTMLButtonElement>;
  onClick?: () => void;
  onPortDown?: (e: ReactPointerEvent) => void;
}) {
  // 프로바이더 색 구분: claude=틸, codex=인디고(그 외는 기본색).
  const provClass = provider === 'claude' || provider === 'codex' ? provider : 'other';
  const statusClass = status ? `status-${status}` : '';
  const active = status === 'assigned';
  const fiveHourPct = usage?.fiveHour?.pct;
  const sub =
    `${krLabel(provider)} · ${krLabel(status)}` +
    (fiveHourPct != null && !Number.isNaN(fiveHourPct)
      ? ` · 5h 잔여 ${Math.round(100 - fiveHourPct)}%`
      : '');

  return (
    <button
      ref={nodeRef}
      type="button"
      className={`topo-node topo-acc ${provClass} ${statusClass}`.trim()}
      data-node-type="account"
      data-node-id={id}
      onClick={onClick}
    >
      <span className="topo-port left" aria-label="연결점" onPointerDown={onPortDown} />
      <span className="topo-acc-avatar" aria-hidden="true">
        <RobotAvatar size={24} />
      </span>
      <span className="topo-acc-body">
        <span className="topo-acc-email">{email}</span>
        <span className="topo-acc-sub">{sub}</span>
      </span>
      {/* 활성(assigned) 계정만 우상단 맥동 점. */}
      {active && <span className="topo-acc-live" aria-hidden="true" />}
      {usage && <AccountUsageDetail usage={usage} />}
    </button>
  );
}

// 호버·포커스 상세 카드 — 5h·7d 잔여율, 리셋 시각, 신선도(관측 경과). 항상
// 렌더하고 CSS(.topo-acc-detail:hover 등)로 표출한다(ServerNode의 .topo-srv-detail
// 과 같은 패턴).
function AccountUsageDetail({ usage }: { usage: AccountUsageSummary }) {
  const rows: { label: string; text: string }[] = [
    { label: '5시간', text: fmtRemainingWindow(usage.fiveHour) },
    { label: '7일', text: fmtRemainingWindow(usage.sevenDay) },
  ];
  return (
    <div className="topo-acc-detail" role="presentation">
      <div className="topo-acc-detail-head">잔여 사용량</div>
      {rows.map((r) => (
        <div className="topo-acc-detail-row" key={r.label}>
          <span>{r.label}</span>
          <span className="mono">{r.text}</span>
        </div>
      ))}
      <div className={`topo-acc-detail-row${usage.stale ? ' stale' : ''}`}>
        <span>관측</span>
        <span className="mono">
          {usage.fetchedAt ? `${relTime(usage.fetchedAt)}${usage.stale ? ' (낡음)' : ''}` : '없음'}
        </span>
      </div>
    </div>
  );
}
