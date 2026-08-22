'use client';

import { useState } from 'react';
import { api } from '@/lib/api-client/client';
import type { Server } from '@/lib/api-client/types';
import { Modal, useAction } from '../common';

// 서버 기본 정보 수정 — 이름·호스트명·소유자(시트 엔진 P1 정책 축). 소유자는
// EditAccountModal.owner와 같은 관례를 쓴다: 빈 문자열로 지우면 조직 공용으로
// 돌아가고(rotation_scope=owner 기본값에서 전 계정을 받는다), 건드리지 않은
// 필드는 서버가 그대로 둔다.
export function EditServer({
  tenantId,
  server,
  onClose,
  onDone,
}: {
  tenantId: string;
  server: Server;
  onClose: () => void;
  onDone: () => void;
}) {
  const [name, setName] = useState(server.name);
  const [hostname, setHostname] = useState(server.hostname ?? '');
  const [owner, setOwner] = useState(server.owner ?? '');
  const act = useAction();
  const nameChanged = name.trim() !== server.name;
  const hostnameChanged = hostname.trim() !== (server.hostname ?? '');
  const ownerChanged = owner.trim() !== (server.owner ?? '');
  const dirty = nameChanged || hostnameChanged || ownerChanged;

  return (
    <Modal title="서버 수정" onClose={onClose}>
      <label>이름</label>
      <input value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" />
      <label>호스트명</label>
      <input value={hostname} onChange={(e) => setHostname(e.target.value)} autoComplete="off" />
      <label>소유자</label>
      <input
        value={owner}
        onChange={(e) => setOwner(e.target.value)}
        autoComplete="off"
        placeholder="담당자·팀 이름 (비우면 지워집니다)"
      />
      <p className="muted" style={{ marginTop: -6 }}>
        비워 두면 조직 공용 — 모든 계정을 받을 수 있습니다.
      </p>
      {act.error && <p className="err">{act.error}</p>}
      <button
        className="primary"
        style={{ marginTop: 14 }}
        disabled={act.busy || !dirty || !name.trim()}
        onClick={() =>
          act.run(
            () =>
              api.updateServer(tenantId, server.id, {
                name: nameChanged ? name.trim() : undefined,
                hostname: hostnameChanged ? hostname.trim() : undefined,
                owner: ownerChanged ? owner.trim() : undefined,
              }),
            onDone,
          )
        }
      >
        저장
      </button>
    </Modal>
  );
}
