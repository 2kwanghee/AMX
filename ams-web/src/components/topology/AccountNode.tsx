'use client';

import type { PointerEvent as ReactPointerEvent, Ref } from 'react';
import { krLabel, RobotAvatar } from '../common';

// 계정 노드 — 에이전트 캡슐. 좌측 연결점(port)에서 드래그해 서버에 연결한다. 노드
// 본문 클릭은 계정 탭 진입. data-node-* 는 드롭 히트테스트에 쓰인다.
//
// 상태 스타일(status-*): assigned=활성(실선+우상단 맥동 점), available=대기(점선),
// quarantined/disabled=붉은 테두리+회색조. usagePct(5h 소진율)는 값이 있을 때만
// 라벨 끝에 붙인다("5h n%").
export function AccountNode({
  id,
  email,
  status,
  provider,
  usagePct,
  nodeRef,
  onClick,
  onPortDown,
}: {
  id: string;
  email: string;
  status?: string;
  provider?: string;
  usagePct?: number;
  nodeRef?: Ref<HTMLButtonElement>;
  onClick?: () => void;
  onPortDown?: (e: ReactPointerEvent) => void;
}) {
  // 프로바이더 색 구분: claude=틸, codex=인디고(그 외는 기본색).
  const provClass = provider === 'claude' || provider === 'codex' ? provider : 'other';
  const statusClass = status ? `status-${status}` : '';
  const active = status === 'assigned';
  const sub =
    `${krLabel(provider)} · ${krLabel(status)}` +
    (usagePct != null && !Number.isNaN(usagePct) ? ` · 5h ${Math.round(usagePct)}%` : '');

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
    </button>
  );
}
