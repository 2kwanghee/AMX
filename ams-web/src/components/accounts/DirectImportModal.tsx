'use client';

import { useState } from 'react';
import { api } from '@/lib/api-client/client';
import { Modal, useAction } from '../common';

export function DirectImport({
  tenantId,
  onClose,
  onDone,
}: {
  tenantId: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const [email, setEmail] = useState('');
  const [secret, setSecret] = useState('');
  const [assignmentExcluded, setAssignmentExcluded] = useState(false);
  const act = useAction();
  return (
    <Modal title="API 키 가져오기" onClose={onClose}>
      <label>이메일</label>
      <input value={email} onChange={(e) => setEmail(e.target.value)} />
      <label>시크릿 (API 키)</label>
      <textarea value={secret} onChange={(e) => setSecret(e.target.value)} rows={3} />
      <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input
          type="checkbox"
          checked={assignmentExcluded}
          onChange={(e) => setAssignmentExcluded(e.target.checked)}
        />
        개인이 자기 프로필에서 직접 쓰는 계정이라 새 배정 대상에서 뺀다
      </label>
      {act.error && <p className="err">{act.error}</p>}
      <button
        className="primary"
        style={{ marginTop: 14 }}
        disabled={act.busy || !email || !secret}
        onClick={() =>
          act.run(
            () =>
              api.createAccount(tenantId, {
                email,
                credentialType: 'api_key',
                secret,
                assignmentExcluded: assignmentExcluded || undefined,
              }),
            onDone,
          )
        }
      >
        가져오기
      </button>
    </Modal>
  );
}
