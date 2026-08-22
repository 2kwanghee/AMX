'use client';

import type { PointerEvent as ReactPointerEvent } from 'react';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { PoolAccount } from '@/lib/api-client/types';
import { coolingProgress, coolingRemainingMs, fmtRemainingPrecise, windowLabel } from '@/lib/pool';
import { Icon, krLabel, RobotAvatar } from '../common';
import type { IconName } from '../common';

// 상황판 우측 계정 풀 레인. 충전중(cooling)·배급처(ready) 두 박스로 나눠 계정 칩을
// 세로로 쌓는다. 충전중 칩은 충전 게이지·맥동 점·호버 툴팁을 달고, 배급처 칩은
// 창별 사용률 툴팁만 단다. AccountNode를 재사용하지 않고 얇은 전용 칩으로 그린다.
// 레인 상자 자체는 TopologyView가 소유한 좌표(x·y·w·h)로 개별 이동·리사이즈되며,
// 헤더가 드래그 핸들, 우하단 그립이 리사이즈 핸들을 겸한다.

type LaneKind = 'cooling' | 'ready';
type LaneRect = { x: number; y: number; w: number; h?: number };
const LANE_ICON: Record<LaneKind, IconName> = { cooling: 'zap', ready: 'user' };
const LANE_TITLE: Record<LaneKind, string> = { cooling: '충전중', ready: '배급처' };

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

// 레인 영역 전체. 충전중 칩이 있을 때만 시계를 돌린다. 각 레인은 rects로 받은
// 독립 좌표·크기에 absolute 배치되고, 드래그·리사이즈 시작은 TopologyView가 준
// 핸들러(onDragStart/onResizeStart)로 위임한다.
export function PoolLanes({
  cooling,
  ready,
  rects,
  dragging,
  resizing,
  onDragStart,
  onResizeStart,
  laneRef,
}: {
  cooling: PoolAccount[];
  ready: PoolAccount[];
  rects: Record<LaneKind, LaneRect>;
  dragging: string | null;
  resizing: string | null;
  onDragStart: (kind: LaneKind) => (e: ReactPointerEvent) => void;
  onResizeStart: (kind: LaneKind) => (e: ReactPointerEvent) => void;
  laneRef: (kind: LaneKind) => (el: HTMLElement | null) => void;
}) {
  const now = useCoolingClock(cooling.length > 0);
  const [fresh, settle] = usePaneArrivals([
    ...cooling.map((a) => ({ id: a.accountId, lane: 'cooling' as const })),
    ...ready.map((a) => ({ id: a.accountId, lane: 'ready' as const })),
  ]);
  return (
    <>
      <PoolLaneBox
        kind="cooling"
        accounts={cooling}
        now={now}
        rect={rects.cooling}
        dragging={dragging === 'lane:cooling'}
        resizing={resizing === 'lane:cooling'}
        onDragStart={onDragStart('cooling')}
        onResizeStart={onResizeStart('cooling')}
        elRef={laneRef('cooling')}
        fresh={fresh}
        settle={settle}
      />
      <PoolLaneBox
        kind="ready"
        accounts={ready}
        now={0}
        rect={rects.ready}
        dragging={dragging === 'lane:ready'}
        resizing={resizing === 'lane:ready'}
        onDragStart={onDragStart('ready')}
        onResizeStart={onResizeStart('ready')}
        elRef={laneRef('ready')}
        fresh={fresh}
        settle={settle}
      />
    </>
  );
}

function PoolLaneBox({
  kind,
  accounts,
  now,
  rect,
  dragging,
  resizing,
  onDragStart,
  onResizeStart,
  elRef,
  fresh,
  settle,
}: {
  kind: LaneKind;
  accounts: PoolAccount[];
  now: number;
  rect: LaneRect;
  dragging: boolean;
  resizing: boolean;
  onDragStart: (e: ReactPointerEvent) => void;
  onResizeStart: (e: ReactPointerEvent) => void;
  elRef: (el: HTMLElement | null) => void;
  fresh: Set<string>;
  settle: (id: string) => void;
}) {
  return (
    <div
      ref={elRef}
      className={`topo-pool-lane ${kind}${rect.h != null ? ' fixed-h' : ''}${dragging ? ' dragging' : ''}${resizing ? ' resizing' : ''}`}
      style={{ left: rect.x, top: rect.y, width: rect.w, height: rect.h }}
    >
      <div className="topo-pool-lane-head" onPointerDown={onDragStart}>
        <span className="topo-pool-lane-icon" aria-hidden="true"><Icon name={LANE_ICON[kind]} size={13} /></span>
        <span className="topo-pool-lane-title">{LANE_TITLE[kind]}</span>
        <span className="topo-pool-lane-count">{accounts.length}</span>
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
      <span
        className="topo-pool-lane-resize"
        aria-hidden="true"
        title="크기 조절"
        onPointerDown={onResizeStart}
      />
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
