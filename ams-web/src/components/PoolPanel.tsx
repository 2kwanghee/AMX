'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
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
  PoolState,
  Recommendation,
} from '@/lib/api-client/types';
import {
  Badge,
  Icon,
  LiveDot,
  Modal,
  ProviderTag,
  SwitchModePill,
  relTime,
  useAction,
  useMarkOnData,
  useNow,
} from './common';
import {
  allowedPoolActions,
  chainStepLabel,
  coolingProgress,
  coolingRemainingMs,
  diffChanged,
  fmtElapsed,
  fmtRemainingPrecise,
  groupAccountsByLane,
  ineligibleReasonLabel,
  isChainActive,
  poolCounts,
  poolEventKindLabel,
  poolStateLabel,
  poolVerbLabel,
  recommendationBasis,
  recommendationKindLabel,
  windowLabel,
} from '@/lib/pool';

const POLL = 30000;
// 카드 막대 두 창 고정 순서. 그 밖의 창은 관측이 있으면 뒤에 이어 붙인다.
const CANON_WINDOWS = ['five_hour', 'seven_day'];
// 대여 중이 아닌 카드에 쓰는 기본 임계(서버 기본 정책과 같은 값).
const DEFAULT_SWAP_AT = 85;
const DEFAULT_PREFETCH_AT = 70;
const SERVER_POLICY_ID = 'pool-server-policy';

// pct → 막대 톤. 교체 임계 이상은 crit, 미리 전달 임계 이상은 warn.
function pctTone(pct: number, swapAt: number, prefetchAt: number): '' | 'warn' | 'crit' {
  if (pct >= swapAt) return 'crit';
  if (pct >= prefetchAt) return 'warn';
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

// 모션 줄이기 설정이면 스크롤도 즉시 이동한다.
function scrollBehavior(): ScrollBehavior {
  if (typeof window === 'undefined') return 'auto';
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
}

// 직전 렌더와 비교해 새로 생긴 id 집합을 돌려준다. 첫 마운트(이전 없음)는
// 건너뛰어 초기 목록은 강조하지 않는다. 강조는 한 번만 치고 animationend에서
// settle(id)로 지운다. 폴링마다 재점화하지 않는다.
function useArrivals(ids: string[]): [Set<string>, (id: string) => void] {
  const prevRef = useRef<string[] | null>(null);
  const [fresh, setFresh] = useState<Set<string>>(new Set());
  const key = ids.join('\n');
  useEffect(() => {
    const prev = prevRef.current;
    prevRef.current = ids;
    if (prev === null) return;
    const { added } = diffChanged(prev, ids);
    if (added.length > 0) setFresh((cur) => new Set([...cur, ...added]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const settle = useCallback((id: string) => {
    setFresh((cur) => {
      if (!cur.has(id)) return cur;
      const next = new Set(cur);
      next.delete(id);
      return next;
    });
  }, []);
  return [fresh, settle];
}

// 카드가 옮겨 온 출발 레인과 시각. 꼬리표(B4)는 POLL 길이만큼 남는다.
interface CardMove {
  from: PoolState;
  at: number;
}

export function PoolPanel({ tenantId }: { tenantId: string }) {
  const { data, error, mutate } = useSWR<PoolOverview>(
    ['pool', tenantId],
    () => api.getPoolOverview(tenantId),
    { refreshInterval: POLL },
  );
  // all로 받아 진행 중과 실패(미확인)를 한 목록에서 나눈다. 실패 체인은 운영자가
  // 확인해야 그 서버의 자동 실행이 다시 열린다.
  const { data: chains, mutate: mutateChains } = useSWR<Chain[]>(
    ['pool-chains', tenantId],
    () => api.listPoolChains(tenantId, 'all'),
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
  const serverOf = new Map(servers.map((s) => [s.serverId, s]));
  const emailOf = new Map(accounts.map((a) => [a.accountId, a.email]));
  const policyOfServer = new Map(servers.map((s) => [s.serverId, s.poolPolicy]));

  // 마지막으로 데이터가 도착한 시각(B6).
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  useEffect(() => {
    if (data !== undefined) setUpdatedAt(Date.now());
  }, [data]);

  // 이전 스냅샷 Map<accountId, poolState>와 비교해 레인을 옮긴 카드를 찾는다(B3).
  // 첫 마운트(이전 없음)는 건너뛴다. 이동 기록은 POLL 길이 뒤에 지운다.
  const prevStateRef = useRef<Map<string, PoolState> | null>(null);
  const [moves, setMoves] = useState<Map<string, CardMove>>(new Map());
  // 삭제 타이머는 다음 폴링이 와도 살아 있어야 꼬리표가 제때 떨어진다. 효과
  // 정리에서 지우지 않고 모아 두었다가 언마운트 때만 일괄 해제한다.
  const moveTimersRef = useRef<Set<ReturnType<typeof setTimeout>>>(new Set());
  useEffect(() => {
    const timers = moveTimersRef.current;
    return () => {
      for (const t of timers) clearTimeout(t);
      timers.clear();
    };
  }, []);
  useEffect(() => {
    if (data === undefined) return;
    const cur = data.accounts;
    const prev = prevStateRef.current;
    prevStateRef.current = new Map(cur.map((a) => [a.accountId, a.poolState]));
    if (prev === null) return;
    const at = Date.now();
    const moved: Array<[string, CardMove]> = [];
    for (const a of cur) {
      const before = prev.get(a.accountId);
      if (before !== undefined && before !== a.poolState) moved.push([a.accountId, { from: before, at }]);
    }
    if (moved.length === 0) return;
    setMoves((m) => new Map([...m, ...moved]));
    const t = setTimeout(() => {
      moveTimersRef.current.delete(t);
      setMoves((m) => {
        const next = new Map(m);
        for (const [id] of moved) if (next.get(id)?.at === at) next.delete(id);
        return next;
      });
    }, POLL);
    moveTimersRef.current.add(t);
  }, [data]);

  // 계정별 최신 state_changed 이벤트의 사유(있으면 꼬리표에 덧붙인다).
  const moveReasonOf = new Map<string, string>();
  for (const e of events ?? []) {
    if (e.kind !== 'state_changed' || !e.accountId || moveReasonOf.has(e.accountId)) continue;
    const reason = e.detail?.reason;
    if (typeof reason === 'string' && reason) moveReasonOf.set(e.accountId, reason);
  }

  // 요약 숫자가 바뀐 항목만 짧게 색을 바꾼다(B5). 값이 같으면 재점화하지 않는다.
  const statIds = (['ready', 'leased', 'cooling', 'pinned', 'held'] as const).map((k) => `${k}:${counts[k]}`);
  const [changedStats, settleStat] = useArrivals(statIds);

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

  function editPolicy(serverId: string) {
    const s = serverOf.get(serverId);
    if (s) setPolicyOf(s);
  }

  const laneProps = {
    now,
    serverNameOf,
    policyOfServer,
    moves,
    moveReasonOf,
    busy: act.busy,
    onAction: doAction,
  };

  const stat = (k: keyof typeof counts, label: string) => {
    const id = `${k}:${counts[k]}`;
    return (
      <span className="pool-stat">
        {label}{' '}
        <b
          className={changedStats.has(id) ? 'changed' : undefined}
          onAnimationEnd={() => settleStat(id)}
        >
          {counts[k]}
        </b>
      </span>
    );
  };

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
          {stat('ready', '배급처')}
          {stat('leased', '대여중')}
          {stat('cooling', '충전소')}
          {stat('pinned', '고정')}
          {stat('held', '보류')}
          <RefreshStatus updatedAt={updatedAt} failed={Boolean(error)} />
          <button disabled={act.busy} onClick={refreshAll} aria-label="새로고침">
            <Icon name="refresh" size={14} />
          </button>
        </div>
      </div>

      {act.error && <p className="err">{act.error}</p>}

      <div className="pool-board">
        <PoolColumn title="배급처" hint="배정 대기" accounts={lanes.ready} {...laneProps} />
        <PoolColumn title="대여중" hint="서버 귀속" accounts={lanes.leased} {...laneProps} />
        <PoolColumn title="충전소" hint="리밋 쿨다운" accounts={lanes.cooling} {...laneProps} />
      </div>

      {(lanes.pinned.length > 0 || lanes.held.length > 0) && (
        <div className="pool-board" style={{ gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', marginTop: 14 }}>
          <PoolColumn title="고정" hint="자동화 제외" accounts={lanes.pinned} {...laneProps} />
          <PoolColumn title="보류" hint="운영자 개입" accounts={lanes.held} {...laneProps} />
        </div>
      )}

      <ServerTable servers={servers} emailOf={emailOf} onPick={setPolicyOf} />

      <div className="pool-aside">
        <RecommendationList
          tenantId={tenantId}
          recommendations={recommendations}
          servers={serverOf}
          onApplied={refreshAll}
          onEditPolicy={editPolicy}
        />
        <ChainList tenantId={tenantId} chains={chains ?? []} serverNameOf={serverNameOf} onAcked={refreshAll} />
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

// -- 마지막 갱신 표시 (B6) ---------------------------------------------------
// 매초 "n초 전"을 올린다. 이 소컴포넌트 안에서만 1초 타이머를 돈다.
function RefreshStatus({ updatedAt, failed }: { updatedAt: number | null; failed: boolean }) {
  const now = useNow(1000);
  if (failed) return <span className="pool-refresh-status failed">갱신 실패 · 재시도 중</span>;
  if (updatedAt === null || now === 0) return null;
  const sec = Math.max(0, Math.floor((now - updatedAt) / 1000));
  return <span className="pool-refresh-status">마지막 갱신 {sec}초 전</span>;
}

// -- 보드 열 -----------------------------------------------------------------
function PoolColumn({
  title,
  hint,
  accounts,
  now,
  serverNameOf,
  policyOfServer,
  moves,
  moveReasonOf,
  busy,
  onAction,
}: {
  title: string;
  hint: string;
  accounts: PoolAccount[];
  now: number;
  serverNameOf: Map<string, string>;
  policyOfServer: Map<string, PoolPolicy>;
  moves: Map<string, CardMove>;
  moveReasonOf: Map<string, string>;
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
          <PoolCard
            key={a.accountId}
            a={a}
            now={now}
            serverNameOf={serverNameOf}
            policy={a.leasedServerId ? policyOfServer.get(a.leasedServerId) : undefined}
            move={moves.get(a.accountId)}
            moveReason={moveReasonOf.get(a.accountId)}
            busy={busy}
            onAction={onAction}
          />
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
  policy,
  move,
  moveReason,
  busy,
  onAction,
}: {
  a: PoolAccount;
  now: number;
  serverNameOf: Map<string, string>;
  policy?: PoolPolicy;
  move?: CardMove;
  moveReason?: string;
  busy: boolean;
  onAction: (accountId: string, verb: PoolAccountVerb) => void;
}) {
  const allowed = allowedPoolActions(a.poolState);
  const serverName = a.leasedServerId ? serverNameOf.get(a.leasedServerId) : undefined;
  const observedAt = lastObservedAt(a);
  // 표준 두 창을 먼저, 그 밖의 관측 창을 뒤에 붙여 중복 없이 나열한다.
  const winById = new Map(a.windows.map((w) => [w.windowId, w]));
  const extraWindows = a.windows.map((w) => w.windowId).filter((id) => !CANON_WINDOWS.includes(id));
  const windowIds = [...CANON_WINDOWS, ...Array.from(new Set(extraWindows))];
  // 대여 서버 정책이 있으면 그 임계로 색을 정하고 눈금선을 그린다(A6).
  const swapAt = policy?.swapAtPct ?? DEFAULT_SWAP_AT;
  const prefetchAt = policy?.prefetchAtPct ?? DEFAULT_PREFETCH_AT;
  const showMarks = a.poolState === 'leased' && policy !== undefined;

  // 도착 하이라이트(B3). 이동 기록이 새로 생길 때 한 번 켜고 animationend에 끈다.
  const [entering, setEntering] = useState(false);
  const moveAt = move?.at;
  useEffect(() => {
    if (moveAt !== undefined) setEntering(true);
  }, [moveAt]);
  void now;

  return (
    <div
      className={`pool-card${entering ? ' pool-card-enter' : ''}`}
      onAnimationEnd={(e) => { if (e.animationName === 'pool-card-enter') setEntering(false); }}
    >
      {move && (
        <div className="pool-card-moved">
          {poolStateLabel(move.from)}에서 이동 · 방금{moveReason ? ` · ${moveReason}` : ''}
        </div>
      )}
      <div className="pool-card-head">
        <span className="pool-card-email" title={a.email}>{a.email}</span>
        <ProviderTag value={a.provider} />
      </div>
      {!a.autoEligible && (
        <div className="pool-badge-off" title="자동화 후보에서 제외된 계정">
          부적격{a.ineligibleReason ? ` · ${ineligibleReasonLabel(a.ineligibleReason)}` : ''}
        </div>
      )}
      {serverName && <div className="pool-card-server">{serverName}</div>}
      {a.poolState === 'cooling' && <CoolingClock a={a} />}
      <div className="pool-win">
        {windowIds.map((id) => {
          const w = winById.get(id);
          if (!w) return null; // 창 자체가 없으면 줄을 그리지 않는다
          const pct = w.pct;
          // 관측을 못 읽은 창(pct null)은 막대 대신 "미상"만 적는다.
          if (pct === null) {
            return (
              <div className="pool-win-row" key={id}>
                <span className="pool-win-name">{windowLabel(id)}</span>
                <span className="pool-win-unknown muted">미상</span>
              </div>
            );
          }
          const tone = pctTone(pct, swapAt, prefetchAt);
          return (
            <div className="pool-win-row" key={id}>
              <span className="pool-win-name">{windowLabel(id)}</span>
              <span className="pool-win-bar">
                <span
                  className={`pool-win-fill ${tone}`}
                  style={{ width: `${Math.min(100, Math.max(0, pct))}%` }}
                />
                {showMarks && (
                  <>
                    <span className="pool-win-mark warn" style={{ left: `${prefetchAt}%` }} title={`미리 전달 임계 ${prefetchAt}%`} />
                    <span className="pool-win-mark crit" style={{ left: `${swapAt}%` }} title={`교체 임계 ${swapAt}%`} />
                  </>
                )}
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

// -- 충전소 시계 (B1·B2) -----------------------------------------------------
// cooling 카드에서만 쓴다. 이 안에서만 1초 타이머가 돌고, 탭이 숨겨지면 틱을
// 멈췄다가 돌아올 때 시각을 맞춘다. 시작·완료 시각이 온전할 때만 게이지를
// 그리고, 완료 시각이 지나면 100% 고정에 맥동을 멈추고 "복귀 대기"로 적는다.
function CoolingClock({ a }: { a: PoolAccount }) {
  const [now, setNow] = useState(0);
  useEffect(() => {
    const tick = () => {
      if (document.visibilityState === 'visible') setNow(Date.now());
    };
    tick();
    const id = setInterval(tick, 1000);
    document.addEventListener('visibilitychange', tick);
    return () => {
      clearInterval(id);
      document.removeEventListener('visibilitychange', tick);
    };
  }, []);
  if (now === 0) return null;
  const remMs = coolingRemainingMs(a.coolingUntil, now);
  const progress = coolingProgress(a, now);
  const done = Boolean(a.coolingUntil) && remMs <= 0;
  const label = a.coolingWindowId ? `${windowLabel(a.coolingWindowId)} 창` : '충전 중';
  return (
    <div className="pool-cool-wrap">
      <div className="pool-cool">
        <Icon name="clock" size={12} />
        {label} · {fmtRemainingPrecise(remMs)}
      </div>
      {progress !== null && (
        <div className="pool-cool-row">
          <div className={`pool-cool-gauge${done ? ' done' : ''}`} aria-hidden>
            <span className="pool-cool-fill" style={{ width: `${progress * 100}%` }} />
          </div>
          <span className="pool-cool-pct">{Math.round(progress * 100)}%</span>
        </div>
      )}
    </div>
  );
}

// -- 서버 표 (A4) ------------------------------------------------------------
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
    <div id={SERVER_POLICY_ID} style={{ marginTop: 18 }}>
      <h2>서버 정책<LiveDot /></h2>
      <p className="muted" style={{ fontSize: 12, marginTop: -4 }}>
        행을 클릭하면 교체 기준을 편집합니다. 창 상한, 관측 유예, 관측 만료는 서버 환경변수에서 바꿉니다.
      </p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>서버</th><th>상태</th><th>모드</th><th>목표 대여</th>
              <th>교체 임계</th><th>미리 전달 임계</th>
              <th>대여 계정</th><th>최대 pct</th><th>수렴</th><th></th>
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
                  <td className="mono">{s.poolPolicy.swapAtPct}%</td>
                  <td className="mono">{s.poolPolicy.prefetchAtPct}%</td>
                  <td className="muted">{emails.length > 0 ? emails.join(', ') : '없음'}</td>
                  <td className="mono">{s.maxPct == null ? '없음' : `${Math.round(s.maxPct)}%`}</td>
                  <td>{s.inFlight ? <span className="warn">진행 중</span> : <span className="muted">대기</span>}</td>
                  <td>
                    <button
                      className="vbtn"
                      aria-label={`${s.name} 교체 기준 편집`}
                      onClick={(e) => { e.stopPropagation(); onPick(s); }}
                    >
                      편집
                    </button>
                  </td>
                </tr>
              );
            })}
            {servers.length === 0 && (
              <tr><td colSpan={10} className="muted">서버가 없습니다.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// -- 권고 목록 (A1·A2·A3·B5) -------------------------------------------------
function RecommendationList({
  tenantId,
  recommendations,
  servers,
  onApplied,
  onEditPolicy,
}: {
  tenantId: string;
  recommendations: Recommendation[];
  servers: Map<string, PoolServer>;
  onApplied: () => void;
  onEditPolicy: (serverId: string) => void;
}) {
  const act = useAction();
  const [freshIds, settle] = useArrivals(recommendations.map((r) => r.id));
  function goPolicy(e: React.MouseEvent) {
    e.preventDefault();
    document.getElementById(SERVER_POLICY_ID)?.scrollIntoView({ behavior: scrollBehavior(), block: 'start' });
  }
  return (
    <div>
      <h2>
        교체 권고<LiveDot />
        <a href={`#${SERVER_POLICY_ID}`} className="pool-policy-link" onClick={goPolicy}>
          서버별 기준은 서버 정책에서 바꿉니다
        </a>
      </h2>
      {recommendations.length === 0 && (
        <p className="muted">
          현재 기준을 넘은 서버가 없습니다.{' '}
          <a href={`#${SERVER_POLICY_ID}`} onClick={goPolicy}>기준은 서버 정책에서 바꿉니다.</a>
        </p>
      )}
      <div className="pool-reco">
        {recommendations.map((r) => {
          const s = servers.get(r.serverId);
          return (
            <div
              className={`pool-reco-item${freshIds.has(r.id) ? ' new' : ''}`}
              key={r.id}
              onAnimationEnd={() => settle(r.id)}
            >
              <div style={{ minWidth: 0 }}>
                <div>
                  <b>{recommendationKindLabel(r.kind)}</b>
                  <span className="muted"> · {s?.name ?? r.serverId.slice(0, 8)}</span>
                </div>
                <div className="pool-reco-basis">
                  {recommendationBasis(r, s?.poolPolicy, s?.leasedAccountIds.length ?? 0)}
                </div>
                <div className="pool-reco-reason">{r.reason}</div>
              </div>
              <div className="pool-reco-btns">
                <button
                  className="vbtn accent"
                  disabled={act.busy}
                  onClick={() => act.run(() => api.applyRecommendation(tenantId, r.id), onApplied)}
                >
                  적용
                </button>
                <button className="vbtn" disabled={!s} onClick={() => onEditPolicy(r.serverId)}>
                  기준 변경
                </button>
              </div>
            </div>
          );
        })}
      </div>
      {act.error && <p className="err">{act.error}</p>}
    </div>
  );
}

// -- 진행 중·실패 체인 -------------------------------------------------------
function ChainList({
  tenantId,
  chains,
  serverNameOf,
  onAcked,
}: {
  tenantId: string;
  chains: Chain[];
  serverNameOf: Map<string, string>;
  onAcked: () => void;
}) {
  const act = useAction();
  const now = useNow(60000);
  const active = chains.filter((c) => isChainActive(c.step));
  // 실패했고 아직 확인하지 않은 체인만 확인 대상으로 남긴다.
  const failed = chains.filter((c) => c.step === 'failed' && !c.ackedAt);
  const [freshIds, settle] = useArrivals([...active, ...failed].map((c) => c.id));
  const srvName = (id: string) => serverNameOf.get(id) ?? id.slice(0, 8);
  return (
    <div>
      <h2>진행 중 체인<LiveDot /></h2>
      {active.length === 0 && failed.length === 0 && (
        <p className="muted">진행 중인 체인이 없습니다.</p>
      )}
      <div className="pool-reco">
        {active.map((c) => (
          <div
            className={`pool-reco-item${freshIds.has(c.id) ? ' new' : ''}`}
            key={c.id}
            onAnimationEnd={() => settle(c.id)}
          >
            <div style={{ minWidth: 0 }}>
              <div>
                <b>{srvName(c.serverId)}</b>
                <span className="pool-chain-step"> · {recommendationKindLabel(c.kind)} · {chainStepLabel(c.step)}</span>
              </div>
              {c.error && <div className="err">{c.error}</div>}
            </div>
            <span className="pool-chain-step">단계 {fmtElapsed(now - new Date(c.stepStartedAt ?? c.startedAt).getTime())} 경과</span>
          </div>
        ))}
        {failed.map((c) => (
          <div
            className={`pool-reco-item${freshIds.has(c.id) ? ' new' : ''}`}
            key={c.id}
            onAnimationEnd={() => settle(c.id)}
          >
            <div style={{ minWidth: 0 }}>
              <div>
                <b>{srvName(c.serverId)}</b>
                <span className="pool-chain-step failed"> · {recommendationKindLabel(c.kind)} · 실패</span>
              </div>
              {c.error && <div className="err">{c.error}</div>}
            </div>
            <button
              className="vbtn"
              disabled={act.busy}
              onClick={() => act.run(() => api.ackPoolChain(tenantId, c.id), onAcked)}
            >
              확인
            </button>
          </div>
        ))}
      </div>
      {act.error && <p className="err">{act.error}</p>}
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

// -- 교체 기준 편집 모달 (A5) ------------------------------------------------
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

  const help = (text: string) => <p className="pool-help muted">{text}</p>;

  return (
    <Modal title={`교체 기준 · ${server.name}`} onClose={onClose}>
      <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>이 값이 권고 생성 기준입니다.</p>
      <label>모드</label>
      <select value={mode} onChange={(e) => setMode(e.target.value as PoolPolicy['mode'])}>
        <option value="manual">수동</option>
        <option value="auto">자동</option>
      </select>
      {help('자동이면 권고를 운영자 확인 없이 실행합니다. 수동이면 권고만 띄웁니다.')}
      <label>목표 대여 수 (1~5)</label>
      <input value={targetLeases} onChange={(e) => setTargetLeases(e.target.value)} inputMode="numeric" />
      {help('이 서버에 붙여 둘 계정 수입니다. 모자라면 배정, 넘치면 초과 회수 권고가 뜹니다.')}
      <label>교체 임계 (%)</label>
      <input value={swapAtPct} onChange={(e) => setSwapAtPct(e.target.value)} inputMode="numeric" />
      {help('대여 계정의 창 사용률이 이 값 이상이면 교체 권고가 뜹니다.')}
      <label>미리 전달 임계 (%)</label>
      <input value={prefetchAtPct} onChange={(e) => setPrefetchAtPct(e.target.value)} inputMode="numeric" />
      {help('이 값 이상이면 교체 전에 다음 계정을 미리 전달하라는 권고가 뜹니다.')}
      <label>최소 대여 시간 (분)</label>
      <input value={minLeaseMinutes} onChange={(e) => setMinLeaseMinutes(e.target.value)} inputMode="numeric" />
      {help('대여를 시작하고 이 시간이 지나기 전에는 교체 권고를 만들지 않습니다.')}
      <label>복귀 임계 (%)</label>
      <input value={readyReturnPct} onChange={(e) => setReadyReturnPct(e.target.value)} inputMode="numeric" />
      {help('충전소 계정의 사용률이 이 값 아래로 내려오면 배급처로 돌아옵니다.')}
      {act.error && <p className="err">{act.error}</p>}
      <button className="primary" style={{ marginTop: 14 }} disabled={act.busy} onClick={save}>
        기준 저장
      </button>
    </Modal>
  );
}
