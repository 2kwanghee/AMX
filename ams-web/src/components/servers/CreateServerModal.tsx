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
  const [mode, setMode] = useState<'auto' | 'manual'>('manual');
  const act = useAction();
  return (
    <Modal title="서버 등록" onClose={onClose}>
      <label>이름</label>
      <input value={name} onChange={(e) => setName(e.target.value)} />
      <label>호스트명</label>
      <input value={hostname} onChange={(e) => setHostname(e.target.value)} />
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
          act.run(() => api.createServer(tenantId, { name, hostname: hostname || undefined, switchMode: mode }), onDone)
        }
      >
        등록
      </button>
    </Modal>
  );
}
