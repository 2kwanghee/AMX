'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import type { PoolAccount } from '@/lib/api-client/types';
import { coolingProgress, coolingRemainingMs, fmtRemainingPrecise, windowLabel } from '@/lib/pool';
import { krLabel, RobotAvatar } from '../common';

// 상황판 우측 계정 풀 레인. 충전중(cooling)·배급처(ready) 두 박스로 나눠 계정 칩을
// 세로로 쌓는다. 충전중 칩은 충전 게이지·맥동 점·호버 툴팁을 달고, 배급처 칩은
// 창별 사용률 툴팁만 단다. AccountNode를 재사용하지 않고 얇은 전용 칩으로 그린다.

type LaneKind = 'cooling' | 'ready';

// 탭이 숨겨지면 틱을 멈추는 1초 시계. 충전중 칩이 있을 때만 active로 켠다
// (PoolPanel의 CoolingClock과 동일 패턴). now=0은 아직 마운트 전(SSR 정합).
function useCoolingClock(active: boolean): number {
  const [now, setNow] = useState(0);
  useEffect(() => {
    if (!active) {
      setNow(0);
      return;
    }
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
  }, [active]);
  return now;
}

// 직전 폴링과 비교해 레인을 옮긴 계정 id를 찾는다. 첫 마운트(이전 없음)는 건너뛰어
// 초기 목록은 강조하지 않는다. 강조는 한 번만 켜고 animationend에서 settle로 지운다.
function usePaneArrivals(items: Array<{ id: string; lane: LaneKind }>): [Set<string>, (id: string) => void] {
  const prevRef = useRef<Map<string, LaneKind> | null>(null);
  const [fresh, setFresh] = useState<Set<string>>(new Set());
  const key = items.map((i) => `${i.id}:${i.lane}`).join('|');
  useEffect(() => {
    const prev = prevRef.current;
    const cur = new Map(items.map((i) => [i.id, i.lane]));
    prevRef.current = cur;
    if (prev === null) return;
    const added: string[] = [];
    for (const [id, lane] of cur) {
      const before = prev.get(id);
      if (before !== undefined && before !== lane) added.push(id);
    }
    if (added.length > 0) setFresh((s) => new Set([...s, ...added]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const settle = useCallback((id: string) => {
    setFresh((s) => {
      if (!s.has(id)) return s;
      const next = new Set(s);
      next.delete(id);
      return next;
    });
  }, []);
  return [fresh, settle];
}

// 레인 영역 전체. 충전중 칩이 있을 때만 시계를 돌린다.
export function PoolLanes({
  cooling,
  ready,
  laneWidth,
}: {
  cooling: PoolAccount[];
  ready: PoolAccount[];
  laneWidth: number;
}) {
  const now = useCoolingClock(cooling.length > 0);
  const [fresh, settle] = usePaneArrivals([
    ...cooling.map((a) => ({ id: a.accountId, lane: 'cooling' as const })),
    ...ready.map((a) => ({ id: a.accountId, lane: 'ready' as const })),
  ]);
  return (
    <>
      <PoolLaneBox title="충전중" kind="cooling" accounts={cooling} now={now} width={laneWidth} fresh={fresh} settle={settle} />
      <PoolLaneBox title="배급처" kind="ready" accounts={ready} now={0} width={laneWidth} fresh={fresh} settle={settle} />
    </>
  );
}

function PoolLaneBox({
  title,
  kind,
  accounts,
  now,
  width,
  fresh,
  settle,
}: {
  title: string;
  kind: LaneKind;
  accounts: PoolAccount[];
  now: number;
  width: number;
  fresh: Set<string>;
  settle: (id: string) => void;
}) {
  return (
    <div className="topo-pool-lane" style={{ width }}>
      <div className="topo-pool-lane-head">
        <span className="topo-pool-lane-title">{title}</span>
        <span className="topo-col-count">{accounts.length}</span>
      </div>
      <div className="topo-pool-cards">
        {accounts.length === 0 && <div className="topo-pool-empty">비어 있음</div>}
        {accounts.map((a, i) => (
          <PoolLaneChip
            key={a.accountId}
            a={a}
            kind={kind}
            now={now}
            // 아래쪽 칩은 툴팁을 위로 뒤집어 캔버스 하단 잘림을 피한다.
            flip={accounts.length > 2 && i >= accounts.length - 2}
            entering={fresh.has(a.accountId)}
            onSettle={() => settle(a.accountId)}
          />
        ))}
      </div>
    </div>
  );
}

function PoolLaneChip({
  a,
  kind,
  now,
  flip,
  entering,
  onSettle,
}: {
  a: PoolAccount;
  kind: LaneKind;
  now: number;
  flip: boolean;
  entering: boolean;
  onSettle: () => void;
}) {
  const provClass = a.provider === 'claude' || a.provider === 'codex' ? a.provider : 'other';
  // 대표 창은 5시간 우선, 없으면 첫 창. pct가 없으면 서브텍스트에 사용률을 빼고 적는다.
  const repWin = a.windows.find((w) => w.windowId === 'five_hour') ?? a.windows[0];
  const repPct = repWin && repWin.pct != null ? Math.round(repWin.pct) : null;
  const sub = krLabel(a.provider) + (repPct != null ? ` · ${windowLabel(repWin!.windowId)} ${repPct}%` : '');

  const cooling = kind === 'cooling';
  const progress = cooling ? coolingProgress(a, now) : null;
  const remMs = cooling ? coolingRemainingMs(a.coolingUntil, now) : 0;
  const done = cooling && Boolean(a.coolingUntil) && progress !== null && remMs <= 0;

  return (
    <div
      className={`topo-pool-chip ${provClass}${flip ? ' flip' : ''}${entering ? ' pool-card-enter' : ''}`.trim()}
      tabIndex={0}
      onAnimationEnd={(e) => {
        if (e.animationName === 'pool-card-enter') onSettle();
      }}
    >
      {cooling && <span className="topo-pool-chip-pulse" aria-hidden="true" />}
      <span className="topo-pool-chip-avatar" aria-hidden="true">
        <RobotAvatar size={20} />
      </span>
      <span className="topo-pool-chip-body">
        <span className="topo-pool-chip-email" title={a.email}>{a.email}</span>
        <span className="topo-pool-chip-sub">{sub}</span>
      </span>
      {cooling && progress !== null && (
        <span className={`topo-pool-gauge${done ? ' done' : ''}`} aria-hidden="true">
          <span className="topo-pool-gauge-fill" style={{ width: `${Math.round(progress * 100)}%` }} />
        </span>
      )}
      <PoolLaneTip a={a} kind={kind} progress={progress} remMs={remMs} done={done} />
    </div>
  );
}

// 호버·포커스 툴팁. 항상 렌더하고 CSS로 표출한다(.topo-srv-detail 패턴).
function PoolLaneTip({
  a,
  kind,
  progress,
  remMs,
  done,
}: {
  a: PoolAccount;
  kind: LaneKind;
  progress: number | null;
  remMs: number;
  done: boolean;
}) {
  if (kind === 'cooling') {
    // 시작·완료 시각이 온전치 않으면 진행률이 null이라 관측값 없음만 적는다.
    if (progress === null) {
      return (
        <div className="topo-pool-tip" role="presentation">
          <div className="topo-pool-tip-row muted">관측값 없음</div>
        </div>
      );
    }
    const finish = a.coolingUntil ? new Date(a.coolingUntil) : null;
    const finishTxt =
      finish && !Number.isNaN(finish.getTime())
        ? finish.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' })
        : null;
    return (
      <div className="topo-pool-tip" role="presentation">
        <div className="topo-pool-tip-head">{a.coolingWindowId ? `${windowLabel(a.coolingWindowId)} 창` : '충전 중'}</div>
        <div className="topo-pool-tip-row">
          <span>충전 진행</span>
          <span className="mono">{done ? '복귀 대기' : `${Math.round(progress * 100)}%`}</span>
        </div>
        <div className="topo-pool-tip-row">
          <span>남은 시간</span>
          <span className="mono">{fmtRemainingPrecise(remMs)}</span>
        </div>
        {finishTxt && (
          <div className="topo-pool-tip-row">
            <span>완료 예정</span>
            <span className="mono">{finishTxt}</span>
          </div>
        )}
      </div>
    );
  }
  // 배급처 — 창별 사용률 나열(관측된 창만).
  const wins = a.windows.filter((w) => w.pct != null);
  return (
    <div className="topo-pool-tip" role="presentation">
      <div className="topo-pool-tip-head">창별 사용률</div>
      {wins.length === 0 && <div className="topo-pool-tip-row muted">관측값 없음</div>}
      {wins.map((w) => (
        <div className="topo-pool-tip-row" key={w.windowId}>
          <span>{windowLabel(w.windowId)}</span>
          <span className="mono">{Math.round(w.pct as number)}%</span>
        </div>
      ))}
    </div>
  );
}
