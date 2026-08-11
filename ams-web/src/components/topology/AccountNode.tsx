'use client';

import type { PointerEvent as ReactPointerEvent, Ref } from 'react';
import { EmailChip, krLabel } from '../common';

// 계정 노드 — 우열. 좌측 연결점(port)에서 드래그해 서버에 연결한다. 노드 본문
// 클릭은 계정 탭 진입. data-node-* 는 드롭 히트테스트에 쓰인다.
export function AccountNode({
  id,
  email,
  status,
  nodeRef,
  onClick,
  onPortDown,
}: {
  id: string;
  email: string;
  status?: string;
  nodeRef?: Ref<HTMLButtonElement>;
  onClick?: () => void;
  onPortDown?: (e: ReactPointerEvent) => void;
}) {
  return (
    <button
      ref={nodeRef}
      type="button"
      className="topo-node topo-acc"
      data-node-type="account"
      data-node-id={id}
      onClick={onClick}
    >
      <span
        className="topo-port left"
        aria-label="연결점"
        onPointerDown={onPortDown}
      />
      <EmailChip email={email} sub={krLabel(status)} />
    </button>
  );
}
