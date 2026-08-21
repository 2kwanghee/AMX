'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import type { PoolAccountVerb } from '@/lib/api-client/client';
import type {
  Chain,
  PoolAccount,
  PoolEvent,
  PoolOverview,
  PoolPolicy,
  PoolServer,
  Recommendation,
} from '@/lib/api-client/types';
import {
  Badge,
  Icon,
  LiveDot,
  Modal,
  ProviderTag,
  SwitchModePill,
  TimeCell,
  relTime,
  useAction,
  useMarkOnData,
  useNow,
} from './common';
import {
  allowedPoolActions,
  chainStepLabel,
  coolingRemainingMs,
  fmtRemaining,
  groupAccountsByLane,
  isChainActive,
  poolCounts,
  poolEventKindLabel,
  poolStateLabel,
  poolVerbLabel,
  recommendationKindLabel,
  windowLabel,
  windowPct,
} from '@/lib/pool';

const POLL = 30000;
// 카드 막대 두 창 고정 순서. 그 밖의 창은 관측이 있으면 뒤에 이어 붙인다.
const CANON_WINDOWS = ['five_hour', 'seven_day'];

// pct → 막대 톤. 교체 임계(85) 이상은 crit, 미리 전달 임계(70) 이상은 warn.
function pctTone(pct: number): '' | 'warn' | 'crit' {
  if (pct >= 85) return 'crit';
  if (pct >= 70) return 'warn';
  return '';
}

// 계정의 마지막 관측 시각(창들의 reportedAt 최댓값).
function lastObservedAt(a: PoolAccount): string | undefined {
  let best = 0;
  let iso: string | undefined;
  for (const w of a.windows) {
    const t = new Date(w.reportedAt).getTime();
    if (!Number.isNaN(t) && t > best) {
      best = t;
      iso = w.reportedAt;
    }
  }
  return iso;
}

export function PoolPanel({ tenantId }: { tenantId: string }) {
  const { data, mutate } = useSWR<PoolOverview>(
    ['pool', tenantId],
    () => api.getPoolOverview(tenantId),
    { refreshInterval: POLL },
  );
  const { data: chains, mutate: mutateChains } = useSWR<Chain[]>(
    ['pool-chains', tenantId],
    () => api.listPoolChains(tenantId, 'active'),
    { refreshInterval: POLL },
  );
  const { data: events, mutate: mutateEvents } = useSWR<PoolEvent[]>(
    ['pool-events', tenantId],
    () => api.listPoolEvents(tenantId, 100),
    { refreshInterval: POLL },
  );
  useMarkOnData(data);
  const act = useAction();
  const now = useNow(60000);
  const [policyOf, setPolicyOf] = useState<PoolServer | null>(null);

  const accounts = data?.accounts ?? [];
  const servers = data?.servers ?? [];
  const recommendations = data?.recommendations ?? [];
  const paused = data?.automationPaused ?? false;
  const counts = poolCounts(accounts);
  const lanes = groupAccountsByLane(accounts);
  const serverNameOf = new Map(servers.map((s) => [s.serverId, s.name]));
  const emailOf = new Map(accounts.map((a) => [a.accountId, a.email]));

  function togglePause() {
    act.run(() => (paused ? api.resumePool(tenantId) : api.pausePool(tenantId)), () => mutate());
  }

  function doAction(accountId: string, verb: PoolAccountVerb) {
    act.run(() => api.poolAccountAction(tenantId, accountId, verb), () => mutate());
  }

  function refreshAll() {
    mutate();
    mutateChains();
    mutateEvents();
  }

  return (
    <div className="panel">
      <div className="pool-topbar">
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <button className={paused ? 'primary' : ''} disabled={act.busy} onClick={togglePause}>
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
              <Icon name={paused ? 'power' : 'pause'} size={14} />
              {paused ? '자동화 재개' : '자동화 일시정지'}
            </span>
          </button>
          {paused && <span className="pool-paused-note">자동화 정지됨</span>}
        </div>
        <div className="pool-summary">
          <span className="pool-stat">배급처 <b>{counts.ready}</b></span>
          <span className="pool-stat">대여중 <b>{counts.leased}</b></span>
          <span className="pool-stat">충전소 <b>{counts.cooling}</b></span>
          <span className="pool-stat">고정 <b>{counts.pinned}</b></span>
          <span className="pool-stat">보류 <b>{counts.held}</b></span>
          <button disabled={act.busy} onClick={refreshAll} aria-label="새로고침">
            <Icon name="refresh" size={14} />
          </button>
        </div>
      </div>

      {act.error && <p className="err">{act.error}</p>}

      <div className="pool-board">
        <PoolColumn title="배급처" hint="배정 대기" accounts={lanes.ready} now={now}
          serverNameOf={serverNameOf} busy={act.busy} onAction={doAction} />
        <PoolColumn title="대여중" hint="서버 귀속" accounts={lanes.leased} now={now}
          serverNameOf={serverNameOf} busy={act.busy} onAction={doAction} />
        <PoolColumn title="충전소" hint="리밋 쿨다운" accounts={lanes.cooling} now={now}
          serverNameOf={serverNameOf} busy={act.busy} onAction={doAction} />
      </div>

      {(lanes.pinned.length > 0 || lanes.held.length > 0) && (
        <div className="pool-board" style={{ gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', marginTop: 14 }}>
          <PoolColumn title="고정" hint="자동화 제외" accounts={lanes.pinned} now={now}
            serverNameOf={serverNameOf} busy={act.busy} onAction={doAction} />
          <PoolColumn title="보류" hint="운영자 개입" accounts={lanes.held} now={now}
            serverNameOf={serverNameOf} busy={act.busy} onAction={doAction} />
        </div>
      )}

      <ServerTable servers={servers} emailOf={emailOf} onPick={setPolicyOf} />

      <div className="pool-aside">
        <RecommendationList
          tenantId={tenantId}
          recommendations={recommendations}
          serverNameOf={serverNameOf}
          onApplied={refreshAll}
        />
        <ChainList chains={chains ?? []} serverNameOf={serverNameOf} />
      </div>

      <EventTimeline events={events ?? []} serverNameOf={serverNameOf} emailOf={emailOf} />

      {policyOf && (
        <PolicyModal
          tenantId={tenantId}
          server={policyOf}
          onClose={() => setPolicyOf(null)}
          onDone={() => { setPolicyOf(null); mutate(); }}
        />
      )}
    </div>
  );
}

// -- 보드 열 -----------------------------------------------------------------
function PoolColumn({
  title,
  hint,
  accounts,
  now,
  serverNameOf,
  busy,
  onAction,
}: {
  title: string;
  hint: string;
  accounts: PoolAccount[];
  now: number;
  serverNameOf: Map<string, string>;
  busy: boolean;
  onAction: (accountId: string, verb: PoolAccountVerb) => void;
}) {
  return (
    <div className="pool-col">
      <div className="pool-col-head">
        <span>{title} <span className="muted" style={{ fontWeight: 400, fontSize: 12 }}>{hint}</span></span>
        <span className="pool-col-count">{accounts.length}</span>
      </div>
      {accounts.length === 0 && <div className="pool-col-empty">비어 있음</div>}
      <div className="pool-cards">
        {accounts.map((a) => (
          <PoolCard key={a.accountId} a={a} now={now} serverNameOf={serverNameOf} busy={busy} onAction={onAction} />
        ))}
      </div>
    </div>
  );
}

// -- 계정 카드 ---------------------------------------------------------------
function PoolCard({
  a,
  now,
  serverNameOf,
  busy,
  onAction,
}: {
  a: PoolAccount;
  now: number;
  serverNameOf: Map<string, string>;
  busy: boolean;
  onAction: (accountId: string, verb: PoolAccountVerb) => void;
}) {
  const allowed = allowedPoolActions(a.poolState);
  const serverName = a.leasedServerId ? serverNameOf.get(a.leasedServerId) : undefined;
  const observedAt = lastObservedAt(a);
  // 표준 두 창을 먼저, 그 밖의 관측 창을 뒤에 붙여 중복 없이 나열한다.
  const extraWindows = a.windows.map((w) => w.windowId).filter((id) => !CANON_WINDOWS.includes(id));
  const windowIds = [...CANON_WINDOWS, ...Array.from(new Set(extraWindows))];
  const coolMs = coolingRemainingMs(a.coolingUntil, now);

  return (
    <div className="pool-card">
      <div className="pool-card-head">
        <span className="pool-card-email" title={a.email}>{a.email}</span>
        <ProviderTag value={a.provider} />
      </div>
      {serverName && <div className="pool-card-server">{serverName}</div>}
      {a.poolState === 'cooling' && (
        <div className="pool-cool">
          <Icon name="clock" size={12} />
          {a.coolingWindowId ? `${windowLabel(a.coolingWindowId)} 창` : '충전 중'} · {fmtRemaining(coolMs)}
        </div>
      )}
      <div className="pool-win">
        {windowIds.map((id) => {
          const pct = windowPct(a, id);
          if (pct === null) return null;
          const tone = pctTone(pct);
          return (
            <div className="pool-win-row" key={id}>
              <span className="pool-win-name">{windowLabel(id)}</span>
              <span className="pool-win-bar">
                <span className={`pool-win-fill ${tone}`} style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
              </span>
              <span className="pool-win-pct">{Math.round(pct)}%</span>
            </div>
          );
        })}
      </div>
      <div className="pool-obs">마지막 관측 {observedAt ? relTime(observedAt) : '기록 없음'}</div>
      {allowed.length > 0 && (
        <div className="pool-card-actions">
          {allowed.map((v) => (
            <button key={v} className="vbtn" disabled={busy} onClick={() => onAction(a.accountId, v)}>
              {poolVerbLabel(v)}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// -- 서버 표 -----------------------------------------------------------------
function ServerTable({
  servers,
  emailOf,
  onPick,
}: {
  servers: PoolServer[];
  emailOf: Map<string, string>;
  onPick: (s: PoolServer) => void;
}) {
  return (
    <div style={{ marginTop: 18 }}>
      <h2>서버 정책<LiveDot /></h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>서버</th><th>상태</th><th>모드</th><th>목표 대여</th>
              <th>대여 계정</th><th>최대 pct</th><th>수렴</th>
            </tr>
          </thead>
          <tbody>
            {servers.map((s) => {
              const emails = s.leasedAccountIds.map((id) => emailOf.get(id) ?? id.slice(0, 8));
              return (
                <tr key={s.serverId} style={{ cursor: 'pointer' }} onClick={() => onPick(s)}>
                  <td>{s.name}</td>
                  <td><Badge value={s.status} /></td>
                  <td><SwitchModePill mode={s.poolPolicy.mode} /></td>
                  <td className="mono">{s.poolPolicy.targetLeases}</td>
                  <td className="muted">{emails.length > 0 ? emails.join(', ') : '없음'}</td>
                  <td className="mono">{s.maxPct == null ? '없음' : `${Math.round(s.maxPct)}%`}</td>
                  <td>{s.inFlight ? <span className="warn">진행 중</span> : <span className="muted">대기</span>}</td>
                </tr>
              );
            })}
            {servers.length === 0 && (
              <tr><td colSpan={7} className="muted">서버가 없습니다.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// -- 권고 목록 ---------------------------------------------------------------
function RecommendationList({
  tenantId,
  recommendations,
  serverNameOf,
  onApplied,
}: {
  tenantId: string;
  recommendations: Recommendation[];
  serverNameOf: Map<string, string>;
  onApplied: () => void;
}) {
  const act = useAction();
  return (
    <div>
      <h2>교체 권고<LiveDot /></h2>
      {recommendations.length === 0 && <p className="muted">권고가 없습니다.</p>}
      <div className="pool-reco">
        {recommendations.map((r) => (
          <div className="pool-reco-item" key={r.id}>
            <div style={{ minWidth: 0 }}>
              <div>
                <b>{recommendationKindLabel(r.kind)}</b>
                <span className="muted"> · {serverNameOf.get(r.serverId) ?? r.serverId.slice(0, 8)}</span>
                {r.triggerPct != null && <span className="muted"> · {Math.round(r.triggerPct)}%</span>}
              </div>
              <div className="pool-reco-reason">{r.reason}</div>
            </div>
            <button
              className="vbtn accent"
              disabled={act.busy}
              onClick={() => act.run(() => api.applyRecommendation(tenantId, r.id), onApplied)}
            >
              적용
            </button>
          </div>
        ))}
      </div>
      {act.error && <p className="err">{act.error}</p>}
    </div>
  );
}

// -- 진행 중 체인 ------------------------------------------------------------
function ChainList({ chains, serverNameOf }: { chains: Chain[]; serverNameOf: Map<string, string> }) {
  const active = chains.filter((c) => isChainActive(c.step));
  return (
    <div>
      <h2>진행 중 체인<LiveDot /></h2>
      {active.length === 0 && <p className="muted">진행 중인 체인이 없습니다.</p>}
      <div className="pool-reco">
        {active.map((c) => (
          <div className="pool-reco-item" key={c.id}>
            <div style={{ minWidth: 0 }}>
              <div>
                <b>{serverNameOf.get(c.serverId) ?? c.serverId.slice(0, 8)}</b>
                <span className={`pool-chain-step ${c.step === 'failed' ? 'failed' : ''}`}> · {chainStepLabel(c.step)}</span>
              </div>
              {c.error && <div className="err">{c.error}</div>}
            </div>
            <TimeCell iso={c.updatedAt} />
          </div>
        ))}
      </div>
    </div>
  );
}

// -- 이벤트 타임라인 ---------------------------------------------------------
function EventTimeline({
  events,
  serverNameOf,
  emailOf,
}: {
  events: PoolEvent[];
  serverNameOf: Map<string, string>;
  emailOf: Map<string, string>;
}) {
  return (
    <div style={{ marginTop: 18 }}>
      <h2>이벤트<LiveDot /></h2>
      {events.length === 0 && <p className="muted">이벤트가 없습니다.</p>}
      <div className="pool-timeline">
        {events.slice(0, 100).map((e) => {
          const who = e.accountId ? emailOf.get(e.accountId) : undefined;
          const where = e.serverId ? serverNameOf.get(e.serverId) : undefined;
          return (
            <div className="pool-timeline-row" key={e.id}>
              <span className="pool-timeline-when">{relTime(e.createdAt)}</span>
              <span className="pool-kind">{poolEventKindLabel(e.kind)}</span>
              <span className="muted">
                {[who, where].filter(Boolean).join(' · ')}
                {e.actor && ` · ${e.actor}`}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// -- 정책 편집 모달 ----------------------------------------------------------
// 6개 필드를 모두 실어 PATCH한다. pct 3종은 0~100, 목표 대여는 1~5, 최소 대여
// 시간은 0~1440분으로 화면에서 먼저 막아 잘못된 값이 서버로 가지 않게 한다.
function PolicyModal({
  tenantId,
  server,
  onClose,
  onDone,
}: {
  tenantId: string;
  server: PoolServer;
  onClose: () => void;
  onDone: () => void;
}) {
  const p = server.poolPolicy;
  const [mode, setMode] = useState<PoolPolicy['mode']>(p.mode);
  const [targetLeases, setTargetLeases] = useState(String(p.targetLeases));
  const [swapAtPct, setSwapAtPct] = useState(String(p.swapAtPct));
  const [prefetchAtPct, setPrefetchAtPct] = useState(String(p.prefetchAtPct));
  const [minLeaseMinutes, setMinLeaseMinutes] = useState(String(p.minLeaseMinutes));
  const [readyReturnPct, setReadyReturnPct] = useState(String(p.readyReturnPct));
  const act = useAction();

  // 정수 파싱 + 범위 검증. 벗어나거나 숫자가 아니면 undefined.
  function parse(v: string, min: number, max: number): number | undefined {
    const n = Number(v);
    if (!Number.isInteger(n) || n < min || n > max) return undefined;
    return n;
  }

  function save() {
    const t = parse(targetLeases, 1, 5);
    const swap = parse(swapAtPct, 0, 100);
    const prefetch = parse(prefetchAtPct, 0, 100);
    const minLease = parse(minLeaseMinutes, 0, 1440);
    const ready = parse(readyReturnPct, 0, 100);
    if (t === undefined) return act.setError('목표 대여 수는 1~5 사이여야 합니다.');
    if (swap === undefined) return act.setError('교체 임계는 0~100 사이여야 합니다.');
    if (prefetch === undefined) return act.setError('미리 전달 임계는 0~100 사이여야 합니다.');
    if (minLease === undefined) return act.setError('최소 대여 시간은 0~1440분 사이여야 합니다.');
    if (ready === undefined) return act.setError('복귀 임계는 0~100 사이여야 합니다.');
    const body: PoolPolicy = {
      mode,
      targetLeases: t,
      swapAtPct: swap,
      prefetchAtPct: prefetch,
      minLeaseMinutes: minLease,
      readyReturnPct: ready,
    };
    return act.run(() => api.updateServerPoolPolicy(tenantId, server.serverId, body), onDone);
  }

  return (
    <Modal title={`풀 정책 · ${server.name}`} onClose={onClose}>
      <label>모드</label>
      <select value={mode} onChange={(e) => setMode(e.target.value as PoolPolicy['mode'])}>
        <option value="manual">수동</option>
        <option value="auto">자동</option>
      </select>
      <label>목표 대여 수 (1~5)</label>
      <input value={targetLeases} onChange={(e) => setTargetLeases(e.target.value)} inputMode="numeric" />
      <label>교체 임계 (%)</label>
      <input value={swapAtPct} onChange={(e) => setSwapAtPct(e.target.value)} inputMode="numeric" />
      <label>미리 전달 임계 (%)</label>
      <input value={prefetchAtPct} onChange={(e) => setPrefetchAtPct(e.target.value)} inputMode="numeric" />
      <label>최소 대여 시간 (분)</label>
      <input value={minLeaseMinutes} onChange={(e) => setMinLeaseMinutes(e.target.value)} inputMode="numeric" />
      <label>복귀 임계 (%)</label>
      <input value={readyReturnPct} onChange={(e) => setReadyReturnPct(e.target.value)} inputMode="numeric" />
      {act.error && <p className="err">{act.error}</p>}
      <button className="primary" style={{ marginTop: 14 }} disabled={act.busy} onClick={save}>
        정책 저장
      </button>
    </Modal>
  );
}
