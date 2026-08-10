'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import type {
  EnrollTokenResponse,
  EventPage,
  Server,
  ServerPage,
  ServerUpdate,
  SwitchStrategy,
  UsageSnapshot,
} from '@/lib/api-client/types';
import { formatEventRow } from '@/lib/event-format';
import { Badge, CopyButton, Modal, fmtTime, krLabel, useAction } from './common';

const POLL = 7000;

export function ServersPanel({ tenantId }: { tenantId: string }) {
  const { data, mutate } = useSWR<ServerPage>(
    ['servers', tenantId],
    () => api.listServers(tenantId),
    { refreshInterval: POLL },
  );
  const [creating, setCreating] = useState(false);
  const [usageOf, setUsageOf] = useState<Server | null>(null);
  const [policyOf, setPolicyOf] = useState<Server | null>(null);
  const [eventsOf, setEventsOf] = useState<Server | null>(null);
  const [tokenOf, setTokenOf] = useState<EnrollTokenResponse | null>(null);
  const act = useAction();
  const servers = data?.items ?? [];

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>서버</h2>
        <button className="primary" onClick={() => setCreating(true)}>서버 등록</button>
      </div>
      {act.error && <p className="err">{act.error}</p>}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>이름</th><th>상태</th><th>전환 모드</th><th className="num">할당 계정</th>
              <th>마지막 접속</th><th>동작</th>
            </tr>
          </thead>
          <tbody>
            {servers.map((s) => (
              <tr key={s.id}>
                <td>{s.name}<div className="muted">{s.hostname}</div></td>
                <td><Badge value={s.status} /></td>
                <td>{krLabel(s.switchMode)}</td>
                <td className="num">{s.assignedAccountCount ?? 0}</td>
                <td className="muted">{fmtTime(s.lastSeenAt)}</td>
                <td>
                  <div className="actions">
                    <button
                      disabled={act.busy}
                      onClick={() =>
                        act.run(
                          () => api.setSwitchMode(tenantId, s.id, s.switchMode === 'auto' ? 'manual' : 'auto'),
                          () => mutate(),
                        )
                      }
                    >
                      {s.switchMode === 'auto' ? '수동 전환으로' : '자동 전환으로'}
                    </button>
                    <button disabled={act.busy} onClick={() => act.run(() => api.refreshUsage(tenantId, s.id))}>
                      사용량 갱신
                    </button>
                    <button onClick={() => setUsageOf(s)}>사용량</button>
                    <button onClick={() => setEventsOf(s)}>이벤트</button>
                    <button onClick={() => setPolicyOf(s)}>정책</button>
                    <button
                      disabled={act.busy}
                      onClick={() =>
                        act.run(async () => {
                          const t = await api.issueEnrollToken(tenantId, s.id);
                          setTokenOf(t);
                        })
                      }
                    >
                      등록 토큰
                    </button>
                    <button
                      className="danger"
                      disabled={act.busy}
                      onClick={() => act.run(() => api.deleteServer(tenantId, s.id), () => mutate())}
                    >
                      삭제
                    </button>
                  </div>
                </td>
              </tr>
            ))}
            {servers.length === 0 && (
              <tr><td colSpan={6} className="muted">등록된 서버가 없습니다. '서버 등록'으로 시작하세요.</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {creating && (
        <CreateServer tenantId={tenantId} onClose={() => setCreating(false)} onDone={() => { setCreating(false); mutate(); }} />
      )}
      {usageOf && <UsageModal tenantId={tenantId} server={usageOf} onClose={() => setUsageOf(null)} />}
      {eventsOf && <EventsModal tenantId={tenantId} server={eventsOf} onClose={() => setEventsOf(null)} />}
      {policyOf && (
        <PolicyModal
          tenantId={tenantId}
          server={policyOf}
          onClose={() => setPolicyOf(null)}
          onDone={() => { setPolicyOf(null); mutate(); }}
        />
      )}
      {tokenOf && (
        <Modal title="등록 토큰 (한 번만 표시)" onClose={() => setTokenOf(null)}>
          <p className="muted">지금 복사하세요 — 다시 조회할 수 없습니다.</p>
          <p style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <code>{tokenOf.token}</code>
            <CopyButton text={tokenOf.token} />
          </p>
          <p className="muted">만료 {fmtTime(tokenOf.expiresAt)}</p>
        </Modal>
      )}
    </div>
  );
}

function CreateServer({
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

// E1 — central policy editor (design §O4). Each numeric field is optional: a
// blank field is submitted as null, which clears the central override and lets
// the agent fall back to its tsamx-local default. Ranges mirror the server-side
// ServerUpdate validation so the UI blocks obviously bad input before the PATCH.
function PolicyModal({
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

// E2 — switch/quarantine/all_exhausted timeline. Rows arrive most recent first,
// as ams-server orders them; formatEventRow (src/lib/event-format.ts) turns each
// raw AccountEvent payload into display fields.
function EventsModal({
  tenantId,
  server,
  onClose,
}: {
  tenantId: string;
  server: Server;
  onClose: () => void;
}) {
  const { data, error } = useSWR<EventPage>(
    ['events', tenantId, server.id],
    () => api.listServerEvents(tenantId, server.id),
    { refreshInterval: POLL },
  );
  const events = data?.items ?? [];
  return (
    <Modal title={`이벤트 — ${server.name}`} onClose={onClose}>
      {error && <p className="muted">이벤트를 불러올 수 없습니다.</p>}
      {!error && events.length === 0 && <p className="muted">아직 이벤트가 없습니다.</p>}
      {events.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>시각</th><th>이벤트</th><th>이전 → 이후</th><th>상세</th></tr>
            </thead>
            <tbody>
              {events.map((ev, i) => {
                const row = formatEventRow(ev);
                return (
                  <tr key={ev.id ?? i}>
                    <td className="muted">{fmtTime(row.reportedAt)}</td>
                    <td>
                      <Badge value={row.kind} />
                      {row.trigger && <span className="muted"> {row.trigger}</span>}
                    </td>
                    <td>{row.transition ?? '—'}</td>
                    <td className="muted">{row.detail}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Modal>
  );
}

function UsageModal({
  tenantId,
  server,
  onClose,
}: {
  tenantId: string;
  server: Server;
  onClose: () => void;
}) {
  const { data, error } = useSWR<UsageSnapshot>(
    ['usage', tenantId, server.id],
    () => api.getUsage(tenantId, server.id),
    { refreshInterval: POLL },
  );
  const p = data?.payload;
  return (
    <Modal title={`사용량 — ${server.name}`} onClose={onClose}>
      {error && <p className="muted">아직 사용량 보고가 없습니다.</p>}
      {p && (
        <>
          <p>
            풀 최대 사용률: <b>{p.poolSummary?.maxUtilizationPct ?? '—'}%</b>{' '}
            {p.poolSummary?.allExhausted && <Badge value="critical" />}
          </p>
          <p className="muted">보고 {fmtTime(data?.reportedAt)}</p>
          <div className="table-wrap">
            <table>
              <thead><tr><th>이메일</th><th>상태</th><th>현재</th></tr></thead>
              <tbody>
                {(p.accounts ?? []).map((a) => (
                  <tr key={a.amsAccountId}>
                    <td>{a.email}</td>
                    <td><Badge value={a.allocationStatus} /></td>
                    <td>{a.isCurrent ? '★' : ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {p.drift && p.drift.length > 0 && (
            <>
              <h3 style={{ marginTop: 12 }}>불일치</h3>
              <ul>{p.drift.map((d, i) => <li key={i} className="err">{d.email || d.amsAccountId}: {d.detail}</li>)}</ul>
            </>
          )}
        </>
      )}
    </Modal>
  );
}
