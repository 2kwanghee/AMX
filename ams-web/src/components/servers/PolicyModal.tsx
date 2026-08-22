'use client';

import { useState } from 'react';
import { api } from '@/lib/api-client/client';
import type { Server, ServerUpdate, SwitchStrategy } from '@/lib/api-client/types';
import { Modal, useAction } from '../common';

// E1 — central policy editor (design §O4). Each numeric field is optional: a
// blank field is submitted as null, which clears the central override and lets
// the agent fall back to its tsamx-local default. Ranges mirror the server-side
// ServerUpdate validation so the UI blocks obviously bad input before the PATCH.
export function PolicyModal({
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
  const numStr = (n?: number | null) => (n === null || n === undefined ? '' : String(n));
  const [threshold, setThreshold] = useState(numStr(server.thresholdPct));
  const [strategy, setStrategy] = useState<'' | SwitchStrategy>(server.defaultStrategy ?? '');
  const [cooldown, setCooldown] = useState(numStr(server.cooldownSeconds));
  const [hysteresis, setHysteresis] = useState(numStr(server.hysteresisPct));
  const act = useAction();

  // '' -> null (clear override); otherwise the parsed number. Returns undefined
  // when the value is present but not a valid number in [min,max].
  function parse(v: string, min: number, max: number): number | null | undefined {
    if (v.trim() === '') return null;
    const n = Number(v);
    if (!Number.isFinite(n) || n < min || n > max) return undefined;
    return n;
  }

  function save() {
    const thresholdPct = parse(threshold, 0, 100);
    const cooldownSeconds = parse(cooldown, 0, 86400);
    const hysteresisPct = parse(hysteresis, 0, 50);
    if (thresholdPct === undefined) return act.setError('전환 임계값은 0–100 사이여야 합니다.');
    if (cooldownSeconds === undefined) return act.setError('쿨다운은 0–86400초 사이여야 합니다.');
    if (hysteresisPct === undefined) return act.setError('히스테리시스는 0–50 사이여야 합니다.');
    const body: ServerUpdate = {
      thresholdPct,
      defaultStrategy: strategy === '' ? null : strategy,
      cooldownSeconds,
      hysteresisPct,
    };
    return act.run(() => api.updateServer(tenantId, server.id, body), onDone);
  }

  return (
    <Modal title={`정책 — ${server.name}`} onClose={onClose}>
      <p className="muted">빈 칸으로 두면 중앙 정책이 해제되고 에이전트가 로컬 기본값을 사용합니다.</p>
      <label>전환 임계값 (%)</label>
      <input value={threshold} onChange={(e) => setThreshold(e.target.value)} placeholder="예: 95" />
      <label>기본 전략</label>
      <select value={strategy} onChange={(e) => setStrategy(e.target.value as '' | SwitchStrategy)}>
        <option value="">(로컬 기본값)</option>
        <option value="best">best</option>
        <option value="next_available">next_available</option>
      </select>
      <label>쿨다운 (초)</label>
      <input value={cooldown} onChange={(e) => setCooldown(e.target.value)} placeholder="예: 300" />
      <label>히스테리시스 (%)</label>
      <input value={hysteresis} onChange={(e) => setHysteresis(e.target.value)} placeholder="예: 5" />
      {act.error && <p className="err">{act.error}</p>}
      <button className="primary" style={{ marginTop: 14 }} disabled={act.busy} onClick={save}>
        정책 저장
      </button>
    </Modal>
  );
}
