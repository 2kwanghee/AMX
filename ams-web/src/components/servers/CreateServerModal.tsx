'use client';

import { useState } from 'react';
import { api } from '@/lib/api-client/client';
import { Modal, useAction } from '../common';

export function CreateServer({
  tenantId,
  onClose,
  onDone,
}: {
  tenantId: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const [name, setName] = useState('');
  const [hostname, setHostname] = useState('');
  const [owner, setOwner] = useState('');
  const [mode, setMode] = useState<'auto' | 'manual'>('manual');
  const act = useAction();
  return (
    <Modal title="서버 등록" onClose={onClose}>
      <label>이름</label>
      <input value={name} onChange={(e) => setName(e.target.value)} />
      <label>호스트명</label>
      <input value={hostname} onChange={(e) => setHostname(e.target.value)} />
      <label>소유자 (선택)</label>
      <input
        value={owner}
        onChange={(e) => setOwner(e.target.value)}
        autoComplete="off"
        placeholder="담당자·팀 이름"
      />
      <p className="muted" style={{ marginTop: -6 }}>
        비워 두면 조직 공용 — 모든 계정을 받을 수 있습니다.
      </p>
      <label>전환 모드</label>
      <select value={mode} onChange={(e) => setMode(e.target.value as 'auto' | 'manual')}>
        <option value="manual">수동</option>
        <option value="auto">자동</option>
      </select>
      {act.error && <p className="err">{act.error}</p>}
      <button
        className="primary"
        style={{ marginTop: 14 }}
        disabled={act.busy || !name}
        onClick={() =>
          act.run(
            () =>
              api.createServer(tenantId, {
                name,
                hostname: hostname || undefined,
                owner: owner.trim() || undefined,
                switchMode: mode,
              }),
            onDone,
          )
        }
      >
        등록
      </button>
    </Modal>
  );
}
