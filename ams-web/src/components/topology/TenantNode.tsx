'use client';

import type { Ref } from 'react';
import { Icon } from '../common';

// 테넌트 노드 — 좌열의 세로 사각. 현재 선택 테넌트 1개. 서버로 향하는 소속선의
// 출발점(정적, 편집 불가). nodeRef로 우측 앵커를 측정한다.
export function TenantNode({
  name,
  serverCount,
  nodeRef,
}: {
  name: string;
  serverCount: number;
  nodeRef?: Ref<HTMLDivElement>;
}) {
  return (
    <div ref={nodeRef} className="topo-tenant" data-node-type="tenant">
      <span className="topo-tenant-icon"><Icon name="grid" size={18} /></span>
      <span className="topo-tenant-name">{name || '테넌트'}</span>
      <span className="topo-tenant-sub">{serverCount}개 서버</span>
    </div>
  );
}
