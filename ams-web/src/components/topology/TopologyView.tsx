'use client';

import type { PointerEvent as ReactPointerEvent } from 'react';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import useSWR, { useSWRConfig } from 'swr';
import { allowedAssignmentActions, api, krApiError } from '@/lib/api-client/client';
import type { AssignmentActionVerb as Verb } from '@/lib/api-client/client';
import type {
  AccountPage,
  AlertPage,
  Assignment,
  AssignmentPage,
  PoolOverview,
  ServerPage,
  TenantPage,
} from '@/lib/api-client/types';
import { accountWindows } from '@/lib/usage-format';
import { groupAccountsByLane } from '@/lib/pool';
import { currentActiveByServer } from '../AssignmentsPanel';
import { Icon, krLabel, markDataArrived, Modal, useAction } from '../common';
import { AccountNode } from './AccountNode';
import { PoolLanes } from './PoolLaneChip';
import { ServerNode } from './ServerNode';
import { StatBar, type StatKey } from './StatBar';
import { TenantNode } from './TenantNode';
import { VERB_ICON, VERB_LABEL, VERB_STYLE } from './verbs';

type EdgeKind = '' | 'active' | 'pending' | 'error';
// 할당 엣지(서버↔계정, 편집 가능). mx/my 는 액션 팝오버 위치(엣지 중점).
type AEdge = { id: string; d: string; kind: EdgeKind; mx: number; my: number };
// 소속 엣지(테넌트→서버, 정적).
type MEdge = { id: string; d: string };
type Ghost = { x0: number; y0: number; x: number; y: number };
type DragFrom = { from: 'server' | 'account'; id: string } | null;

// -- 자유 배치(P1) ----------------------------------------------------------
// 노드 좌표 소유권을 CSS Grid → JS 상태로. 서버·계정 노드만 이동, 테넌트는 좌측
// 고정. 좌표는 gridRef(전체 3열 컨테이너) 기준 캔버스 영역 로컬 픽셀.
type Pos = { x: number; y: number };
type Layout = Record<string, Pos>; // key: `srv:<id>` | `acc:<id>`
type Dragging = { key: string; pos: Pos } | null;

const GRID = 24; // 스냅 격자(px). 설정 노출은 후속.
const NODE_W = 264; // 자유 배치 시 노드 폭(= 11*GRID)
const SERVER_X = GRID; // 서버 밴드 좌측 x
const ACCOUNT_X = SERVER_X + NODE_W + 4 * GRID; // 계정 밴드 좌측 x (사이 96px)
const BAND_TOP = 2 * GRID; // 밴드 라벨 아래 첫 노드 y
// NMS 리뉴얼로 서버 노드는 더 작고(≈156px), 계정 노드는 캡슐(≈52px)로 얇아졌다.
// 자동 안착 간격을 실측 높이 + 여백에 맞춰 좁혀 밀도를 높인다.
const SERV_STEP = 7 * GRID; // 서버 노드 세로 간격(168px)
const ACC_STEP = 3 * GRID; // 계정 노드 세로 간격(72px)
const CANVAS_PAD = 2 * GRID; // 캔버스 하단 여백
// 계정 풀 레인 — 계정 열 오른쪽에 세로 박스 2개(충전중·배급처). POOL_X는 계정 열
// 오른쪽 96px. LANE_GAP은 PoolLaneChip.tsx의 .topo-pool-lanes gap과 같아야 한다.
const POOL_X = ACCOUNT_X + NODE_W + 4 * GRID; // 744
const LANE_W = 9 * GRID; // 216 (≈ NODE_W)
const LANE_GAP = 2 * GRID; // 48
const LANE_TOP = 8; // 밴드 라벨과 대략 정렬
const POOL_RIGHT = POOL_X + 2 * LANE_W + LANE_GAP + CANVAS_PAD; // 레인 포함 캔버스 우측 끝
const FREE_MIN = 900; // 이 폭 미만이면 자동 배치(현행 grid) 폴백
const LAYOUT_VERSION = 1;
// 미측정 노드 기본 크기(높이 추정) — 첫 렌더/드롭 겹침 검사 폴백.
const DEF_SIZE: Record<'srv' | 'acc', { w: number; h: number }> = {
  srv: { w: NODE_W, h: 156 },
  acc: { w: NODE_W, h: 52 },
};

const snap = (v: number) => Math.round(v / GRID) * GRID;
const layoutStorageKey = (tenantId: string) => `amx.topo.layout.${tenantId}`;

function loadLayout(tenantId: string): Layout {
  if (typeof window === 'undefined') return {};
  try {
    const raw = window.localStorage.getItem(layoutStorageKey(tenantId));
    if (!raw) return {};
    const parsed = JSON.parse(raw) as { version?: number; nodes?: unknown };
    if (parsed?.version !== LAYOUT_VERSION || !parsed.nodes || typeof parsed.nodes !== 'object') return {};
    // 값 검증 — {x,y}가 유한수인 항목만 채택(손상·비정상 값은 버림).
    const out: Layout = {};
    for (const [k, v] of Object.entries(parsed.nodes as Record<string, unknown>)) {
      if (v && typeof v === 'object') {
        const { x, y } = v as { x?: unknown; y?: unknown };
        if (Number.isFinite(x) && Number.isFinite(y)) out[k] = { x: x as number, y: y as number };
      }
    }
    return out;
  } catch {
    return {};
  }
}

function saveLayout(tenantId: string, nodes: Layout) {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(
      layoutStorageKey(tenantId),
      JSON.stringify({ version: LAYOUT_VERSION, nodes }),
    );
  } catch {
    // 저장 실패(용량·프라이빗 모드)는 무시 — 배치는 세션 내 유지된다.
  }
}

// 두 박스가 margin 여유를 두고 겹치는지(AABB).
function overlaps(ap: Pos, as: { w: number; h: number }, bp: Pos, bs: { w: number; h: number }, m = 8): boolean {
  return (
    ap.x - m < bp.x + bs.w &&
    ap.x + as.w + m > bp.x &&
    ap.y - m < bp.y + bs.h &&
    ap.y + as.h + m > bp.y
  );
}

// 마스터 콘솔 토폴로지 편집기. 좌 테넌트 · 중 서버 · 우 계정 3열. 테넌트→서버는
// 정적 소속선, 서버↔계정은 편집 가능한 할당선. 포트 드래그로 연결 생성, 선 클릭으로
// 동작 팝오버. 데이터는 각 표 패널과 동일한 SWR 키/주기를 재사용한다.
export function TopologyView({ tenantId, onGo }: { tenantId: string; onGo: (t: StatKey) => void }) {
  const { data: tenantsData } = useSWR<TenantPage>('tenants', () => api.listTenants());
  const { data: serversData } = useSWR<ServerPage>(['servers', tenantId], () => api.listServers(tenantId), {
    refreshInterval: 7000,
    onSuccess: () => markDataArrived(),
  });
  const { data: accountsData } = useSWR<AccountPage>(['accounts', tenantId], () => api.listAccounts(tenantId), {
    refreshInterval: 8000,
    onSuccess: () => markDataArrived(),
  });
  const { data: assignData } = useSWR<AssignmentPage>(['assignments', tenantId], () => api.listAssignments(tenantId), {
    refreshInterval: 6000,
    onSuccess: () => markDataArrived(),
  });
  const { data: alertsData } = useSWR<AlertPage>(['alerts', tenantId, 'open'], () => api.listAlerts(tenantId, 'open'), {
    refreshInterval: 7000,
    onSuccess: () => markDataArrived(),
  });
  // 계정 풀 개요 — PoolPanel과 SWR 키·주기를 공유해 캐시를 재사용한다. 첫 로딩
  // 전에는 레인을 그리지 않고, 이후 폴링 실패 시엔 마지막 데이터로 유지한다.
  const { data: poolData } = useSWR<PoolOverview>(
    ['pool', tenantId],
    () => api.getPoolOverview(tenantId),
    { refreshInterval: 30000 },
  );

  const servers = serversData?.items ?? [];
  const accounts = accountsData?.items ?? [];
  const assignments = assignData?.items ?? [];
  const tenantName = (tenantsData?.items ?? []).find((t) => t.id === tenantId)?.name ?? '';

  // 결정적 순서: 서버 이름순. 계정은 소속 서버 인덱스 → 이메일 순(미할당은 뒤로).
  const orderedServers = [...servers].sort((a, b) => a.name.localeCompare(b.name, 'ko'));
  const serverIndex = new Map(orderedServers.map((s, i) => [s.id, i]));
  const serverName = new Map(servers.map((s) => [s.id, s.name]));
  const emailOf = new Map(accounts.map((a) => [a.id, a.email]));
  const providerOf = new Map(accounts.map((a) => [a.id, a.provider]));
  const activeByServer = currentActiveByServer(assignments, accounts);

  // 계정 5h 소진율 배선 — 각 온라인 서버의 usage payload(accounts[].windows 중
  // window_minutes=300, 없으면 five_hour 폴백)를 계정 이메일로 모아 계정 노드
  // 서브라인에 전달한다. 계정은 서버당 유일 할당이라 이메일→pct 는 충돌 없이
  // 결정된다. 값이 없으면 undefined 그대로 두어 서브라인은 프로바이더·상태만
  // 표시(기존 degrade 유지).
  const onlineIds = orderedServers.filter((s) => s.status === 'online').map((s) => s.id);
  const { data: usageSnaps } = useSWR(
    onlineIds.length ? ['topo-usage', tenantId, onlineIds.join(',')] : null,
    () => Promise.all(onlineIds.map((id) => api.getUsage(tenantId, id).catch(() => null))),
    { refreshInterval: 30000, shouldRetryOnError: false },
  );
  const usagePctByEmail = new Map<string, number>();
  for (const snap of usageSnaps ?? []) {
    for (const acc of snap?.payload?.accounts ?? []) {
      const email = acc.account?.email;
      const pct = accountWindows(acc).find((w) => w.windowMinutes === 300)?.pct;
      if (email && pct != null) usagePctByEmail.set(email, pct);
    }
  }

  const minServerIdx = new Map<string, number>();
  for (const a of assignments) {
    if (a.state === 'detached') continue; // 회수된 연결은 정렬 기준에서 제외
    const idx = serverIndex.get(a.serverId);
    if (idx === undefined) continue;
    const cur = minServerIdx.get(a.accountId);
    if (cur === undefined || idx < cur) minServerIdx.set(a.accountId, idx);
  }
  const orderedAccounts = [...accounts].sort((a, b) => {
    const ia = minServerIdx.has(a.id) ? minServerIdx.get(a.id)! : Number.MAX_SAFE_INTEGER;
    const ib = minServerIdx.has(b.id) ? minServerIdx.get(b.id)! : Number.MAX_SAFE_INTEGER;
    if (ia !== ib) return ia - ib;
    return a.email.localeCompare(b.email, 'ko');
  });

  // -- 측정 & 상태 ----------------------------------------------------------
  const gridRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const nodeRefs = useRef<Map<string, HTMLElement>>(new Map());
  const [aEdges, setAEdges] = useState<AEdge[]>([]);
  const [mEdges, setMEdges] = useState<MEdge[]>([]);
  const [size, setSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 });
  const [ghost, setGhost] = useState<Ghost | null>(null);
  const [dragFrom, setDragFrom] = useState<DragFrom>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [busyEdge, setBusyEdge] = useState<string | null>(null);
  const [connect, setConnect] = useState<{ accountId: string; serverId: string } | null>(null);
  // 배정 제외 계정을 드래그했을 때 띄우는 안내. 어떤 요청도 보내지 않고 즉시
  // 멎어야 하는 게이트라 connect(ConnectModal)와 별도 상태로 둔다 — recall이
  // 나가는 move()로 흘러들지 않게 handleDrop에서 여기서 끊는다.
  const [excludedNotice, setExcludedNotice] = useState<string | null>(null);

  const setNode = useCallback((id: string) => (el: HTMLElement | null) => {
    if (el) nodeRefs.current.set(id, el);
    else nodeRefs.current.delete(id);
  }, []);

  // -- 자유 배치 상태 -------------------------------------------------------
  const [layout, setLayout] = useState<Layout>({});
  const [dragging, setDragging] = useState<Dragging>(null);
  const [freeMode, setFreeMode] = useState(false);
  const [canvasH, setCanvasH] = useState(0);
  // 레인 영역 높이 측정 — 노드 높이(canvasH)와 별개로 캔버스 최소 높이에 반영해
  // 레인이 노드보다 길어도 하단이 잘리지 않게 한다.
  const laneRef = useRef<HTMLDivElement | null>(null);
  const [laneBottom, setLaneBottom] = useState(0);
  const draggingRef = useRef<Dragging>(null);
  const suppressClickRef = useRef(false);
  // 측정된 노드 크기(px) — 겹침 검사·캔버스 높이 계산에 사용. measure()에서 갱신.
  const nodeSize = useRef<Map<string, { w: number; h: number }>>(new Map());

  const setDrag = useCallback((v: Dragging) => {
    draggingRef.current = v;
    setDragging(v);
  }, []);

  // 테넌트 전환 시 저장된 배치를 로드(신규 테넌트는 빈 맵).
  useEffect(() => {
    setDrag(null);
    setLayout(loadLayout(tenantId));
  }, [tenantId, setDrag]);

  // 현재 노드 집합에 없는 저장 키 가지치기. 데이터 로드 후에만 수행(로딩 중
  // 빈 목록으로 전량 삭제되는 것을 막는다).
  const nodeIdKey = servers.map((s) => s.id).join(',') + '#' + accounts.map((a) => a.id).join(',');
  useEffect(() => {
    if (!serversData || !accountsData) return;
    const valid = new Set<string>();
    for (const s of servers) valid.add(`srv:${s.id}`);
    for (const a of accounts) valid.add(`acc:${a.id}`);
    setLayout((prev) => {
      let changed = false;
      const next: Layout = {};
      for (const [k, v] of Object.entries(prev)) {
        if (valid.has(k)) next[k] = v;
        else changed = true;
      }
      if (!changed) return prev;
      saveLayout(tenantId, next);
      return next;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, serversData, accountsData, nodeIdKey]);

  // 좌표 없는 노드의 자동 안착 위치 — 현행 정렬 순서를 열별로 세로 배치.
  const autoPos = useMemo(() => {
    const m = new Map<string, Pos>();
    orderedServers.forEach((s, i) => m.set(`srv:${s.id}`, { x: SERVER_X, y: snap(BAND_TOP + i * SERV_STEP) }));
    orderedAccounts.forEach((a, i) => m.set(`acc:${a.id}`, { x: ACCOUNT_X, y: snap(BAND_TOP + i * ACC_STEP) }));
    return m;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orderedServers.map((s) => s.id).join(','), orderedAccounts.map((a) => a.id).join(',')]);

  const effectivePos = useCallback(
    (key: string): Pos => {
      if (draggingRef.current?.key === key) return draggingRef.current.pos;
      return layout[key] ?? autoPos.get(key) ?? { x: 0, y: 0 };
    },
    [layout, autoPos],
  );

  const sizeOf = (key: string) =>
    nodeSize.current.get(key) ?? DEF_SIZE[key.startsWith('srv:') ? 'srv' : 'acc'];

  // 드롭 지점에서 가장 가까운 빈 스냅 위치로 밀어내기(격자 나선 탐색, 상한 있음).
  const resolveOverlap = useCallback(
    (key: string, pos: Pos): Pos => {
      const size = sizeOf(key);
      const others: { pos: Pos; size: { w: number; h: number } }[] = [];
      for (const s of orderedServers) if (`srv:${s.id}` !== key) others.push({ pos: effectivePos(`srv:${s.id}`), size: sizeOf(`srv:${s.id}`) });
      for (const a of orderedAccounts) if (`acc:${a.id}` !== key) others.push({ pos: effectivePos(`acc:${a.id}`), size: sizeOf(`acc:${a.id}`) });
      const free = (p: Pos) => !others.some((o) => overlaps(p, size, o.pos, o.size));
      if (free(pos)) return pos;
      // 세로 우선 탐색(열 정렬 유지), 실패 시 가로까지 확장.
      for (let r = 1; r <= 60; r++) {
        for (const dy of [r, -r]) {
          const p = { x: pos.x, y: Math.max(0, pos.y + dy * GRID) };
          if (free(p)) return p;
        }
      }
      for (let rx = 1; rx <= 20; rx++) {
        for (const dx of [rx, -rx]) {
          for (let ry = 0; ry <= 60; ry++) {
            for (const dy of ry === 0 ? [0] : [ry, -ry]) {
              const p = { x: Math.max(0, pos.x + dx * GRID), y: Math.max(0, pos.y + dy * GRID) };
              if (free(p)) return p;
            }
          }
        }
      }
      return pos; // 탐색 실패 시 그대로(겹침 허용) — 데이터 손실보다 낫다.
    },
    [orderedServers, orderedAccounts, effectivePos],
  );

  const commitPos = useCallback(
    (key: string, pos: Pos) => {
      setLayout((prev) => {
        const next = { ...prev, [key]: pos };
        saveLayout(tenantId, next);
        return next;
      });
    },
    [tenantId],
  );

  const resetLayout = useCallback(() => {
    setDrag(null);
    setLayout({});
    if (typeof window !== 'undefined') {
      try {
        window.localStorage.removeItem(layoutStorageKey(tenantId));
      } catch {
        /* noop */
      }
    }
  }, [tenantId, setDrag]);

  // 노드 본문 드래그 이동 — 포트(연결 생성) 위 시작은 제외한다.
  const startNodeDrag = (kind: 'srv' | 'acc', id: string) => (e: ReactPointerEvent) => {
    if (!freeMode) return;
    if ((e.target as HTMLElement).closest('.topo-port')) return;
    const key = `${kind}:${id}`;
    const startX = e.clientX;
    const startY = e.clientY;
    const origin = effectivePos(key);
    let moved = false;
    let last: Pos | null = null;
    suppressClickRef.current = false;

    const move = (ev: PointerEvent) => {
      const dx = ev.clientX - startX;
      const dy = ev.clientY - startY;
      if (!moved && Math.hypot(dx, dy) > 4) moved = true;
      if (!moved) return;
      suppressClickRef.current = true;
      // x는 [0, 캔버스폭−노드폭]로 클램프(가로 이탈 방지, 캔버스 확장 안 함).
      const maxX = Math.max(0, (canvasRef.current?.clientWidth ?? Number.POSITIVE_INFINITY) - NODE_W);
      const nx = Math.min(maxX, Math.max(0, snap(origin.x + dx)));
      const ny = Math.max(0, snap(origin.y + dy));
      // 스냅 좌표가 직전과 같으면 리렌더·measure 리플로 스킵.
      if (last && last.x === nx && last.y === ny) return;
      last = { x: nx, y: ny };
      setDrag({ key, pos: last });
    };
    const finish = (commit: boolean) => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      window.removeEventListener('pointercancel', cancel);
      window.removeEventListener('keydown', keydown);
      const d = draggingRef.current;
      setDrag(null);
      if (commit && d && moved) commitPos(d.key, resolveOverlap(d.key, d.pos));
    };
    const up = () => finish(true);
    const cancel = () => finish(false);
    const keydown = (ev: KeyboardEvent) => { if (ev.key === 'Escape') finish(false); };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', cancel);
    window.addEventListener('keydown', keydown);
  };

  // 드래그 직후의 click(내비게이션)을 1회 무시한다.
  const guardClick = (fn: () => void) => () => {
    if (suppressClickRef.current) {
      suppressClickRef.current = false;
      return;
    }
    fn();
  };

  const edgeSpecs = assignments
    .filter((a) => a.state !== 'detached' && serverIndex.has(a.serverId) && emailOf.has(a.accountId))
    .map((a) => {
      let kind: EdgeKind = '';
      if (a.lastError) kind = 'error';
      else if (a.pendingCommandId || busyEdge === a.id) kind = 'pending';
      else if (activeByServer.get(a.serverId) === a.accountId) kind = 'active';
      return { id: a.id, accId: a.accountId, srvId: a.serverId, kind };
    });

  // measure 재실행 트리거 — 노드 유효 좌표·자유배치 여부가 바뀌면 엣지·캔버스 재측정.
  const posKey =
    `free:${freeMode}|` +
    orderedServers.map((s) => { const p = effectivePos(`srv:${s.id}`); return `${s.id}@${p.x},${p.y}`; }).join(';') +
    '#' +
    orderedAccounts.map((a) => { const p = effectivePos(`acc:${a.id}`); return `${a.id}@${p.x},${p.y}`; }).join(';');

  const measure = useCallback(() => {
    const grid = gridRef.current;
    if (!grid) return;
    const base = grid.getBoundingClientRect();
    const rel = (el: HTMLElement) => {
      const r = el.getBoundingClientRect();
      return { l: r.left - base.left, r: r.right - base.left, cy: r.top - base.top + r.height / 2 };
    };
    // 소속선: 테넌트 우측(세로 중앙)에서 각 서버 좌측으로 부채꼴.
    const tenantEl = nodeRefs.current.get('tenant');
    const nextM: MEdge[] = [];
    if (tenantEl) {
      const tr = tenantEl.getBoundingClientRect();
      const tx = tr.right - base.left;
      const ty = tr.top - base.top + tr.height / 2;
      for (const s of orderedServers) {
        const el = nodeRefs.current.get(`srv:${s.id}`);
        if (!el) continue;
        const p = rel(el);
        const dx = Math.max(30, Math.abs(p.l - tx) * 0.5);
        nextM.push({ id: s.id, d: `M${tx},${ty} C${tx + dx},${ty} ${p.l - dx},${p.cy} ${p.l},${p.cy}` });
      }
    }
    // 할당선: 서버 우측 → 계정 좌측.
    const nextA: AEdge[] = [];
    for (const spec of edgeSpecs) {
      const srvEl = nodeRefs.current.get(`srv:${spec.srvId}`);
      const accEl = nodeRefs.current.get(`acc:${spec.accId}`);
      if (!srvEl || !accEl) continue;
      const sp = rel(srvEl);
      const ap = rel(accEl);
      const x1 = sp.r, y1 = sp.cy, x2 = ap.l, y2 = ap.cy;
      const dx = Math.max(40, Math.abs(x2 - x1) * 0.4);
      const c1x = x1 + dx, c2x = x2 - dx;
      // 큐빅 베지어 중점 t=0.5.
      const mx = 0.125 * x1 + 0.375 * c1x + 0.375 * c2x + 0.125 * x2;
      const my = 0.125 * y1 + 0.375 * y1 + 0.375 * y2 + 0.125 * y2;
      nextA.push({ id: spec.id, kind: spec.kind, mx, my, d: `M${x1},${y1} C${c1x},${y1} ${c2x},${y2} ${x2},${y2}` });
    }
    // 노드 크기 기록(자유 배치 겹침·캔버스 높이 계산용).
    for (const [k, el] of nodeRefs.current) {
      const r = el.getBoundingClientRect();
      if (r.width && r.height) nodeSize.current.set(k, { w: r.width, h: r.height });
    }
    const wide = grid.offsetWidth >= FREE_MIN;
    setFreeMode(wide);
    // 자유 배치는 absolute라 컨테이너가 붕괴 — 노드 최하단 + 여백으로 높이 산정.
    let bottom = 0;
    if (wide) {
      for (const s of orderedServers) bottom = Math.max(bottom, effectivePos(`srv:${s.id}`).y + (nodeSize.current.get(`srv:${s.id}`)?.h ?? DEF_SIZE.srv.h));
      for (const a of orderedAccounts) bottom = Math.max(bottom, effectivePos(`acc:${a.id}`).y + (nodeSize.current.get(`acc:${a.id}`)?.h ?? DEF_SIZE.acc.h));
      bottom += CANVAS_PAD;
    }
    setCanvasH(bottom);
    setMEdges(nextM);
    setAEdges(nextA);
    setSize({ w: grid.offsetWidth, h: Math.max(grid.offsetHeight, bottom) });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layoutKey(edgeSpecs, orderedServers.map((s) => s.id)) + '§' + posKey]);

  useLayoutEffect(() => {
    measure();
    const grid = gridRef.current;
    if (!grid || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => measure());
    ro.observe(grid);
    return () => ro.disconnect();
  }, [measure]);

  // -- 드래그로 연결 생성 ---------------------------------------------------
  const startDrag = (from: 'server' | 'account', id: string) => (e: ReactPointerEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const grid = gridRef.current;
    if (!grid) return;
    const base = grid.getBoundingClientRect();
    const pr = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const x0 = pr.left + pr.width / 2 - base.left;
    const y0 = pr.top + pr.height / 2 - base.top;
    setSelected(null);
    setDragFrom({ from, id });
    setGhost({ x0, y0, x: e.clientX - base.left, y: e.clientY - base.top });

    const cleanup = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      window.removeEventListener('pointercancel', cancel);
      window.removeEventListener('keydown', key);
      setGhost(null);
      setDragFrom(null);
    };
    const move = (ev: PointerEvent) => {
      const b = grid.getBoundingClientRect();
      setGhost({ x0, y0, x: ev.clientX - b.left, y: ev.clientY - b.top });
    };
    const up = (ev: PointerEvent) => {
      const el = document.elementFromPoint(ev.clientX, ev.clientY) as HTMLElement | null;
      const nodeEl = el?.closest('[data-node-id]') as HTMLElement | null;
      const tType = nodeEl?.getAttribute('data-node-type') ?? null;
      const tId = nodeEl?.getAttribute('data-node-id') ?? null;
      cleanup();
      handleDrop(from, id, tType, tId);
    };
    // pointercancel(제스처 취소)·Esc는 드롭 없이 정리만 한다.
    const cancel = () => cleanup();
    const key = (ev: KeyboardEvent) => { if (ev.key === 'Escape') cleanup(); };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', cancel);
    window.addEventListener('keydown', key);
  };

  function handleDrop(from: 'server' | 'account', id: string, tType: string | null, tId: string | null) {
    if (!tType || !tId) return;
    let serverId: string | undefined;
    let accountId: string | undefined;
    if (from === 'server' && tType === 'account') { serverId = id; accountId = tId; }
    else if (from === 'account' && tType === 'server') { serverId = tId; accountId = id; }
    else return; // 같은 유형 위 드롭은 무효
    // 배정 제외 계정은 이동 경로(move: recall → createAssignment)든 신규 연결
    // 경로(confirm: createAssignment)든 여기서 끊는다. move()는 recall을 먼저
    // 내보내는데 회수는 항상 purge라 되돌릴 수 없고, 그 뒤에 오는 409는 이미
    // 늦다 — 그래서 어느 API 호출로도 진입하기 전에, ConnectModal을 열기 전에
    // 계정 목록(이미 로드돼 있음)만으로 판정한다.
    if (accounts.find((a) => a.id === accountId)?.assignmentExcluded) {
      setExcludedNotice(
        '이 계정은 할당 대상에서 제외돼 있어 여기로 할당할 수 없습니다. 계정 편집에서 제외를 해제한 뒤 다시 시도하세요.',
      );
      return;
    }
    // 이미 같은 서버에 비-detached로 연결돼 있으면 무효(중복 생성 안 함). 회수된
    // 이력 행은 중복으로 보지 않아 같은 서버로의 재연결 드롭이 막히지 않는다.
    if (assignments.some((a) => a.accountId === accountId && a.serverId === serverId && a.state !== 'detached')) return;
    setConnect({ accountId, serverId });
  }

  // 레인 데이터 — 개요가 한 번이라도 로드됐고 자유 배치일 때 그린다. 이후 폴링이
  // 일시 실패해도 SWR이 쥔 마지막 데이터로 유지해, 30초마다 레인이 깜빡이지 않는다.
  const poolLanes = poolData ? groupAccountsByLane(poolData.accounts) : null;
  const showLanes = freeMode && poolLanes !== null;
  const poolKey = poolLanes
    ? poolLanes.cooling.map((a) => a.accountId).join(',') + '#' + poolLanes.ready.map((a) => a.accountId).join(',')
    : '';

  // 레인 컨테이너 하단(offsetTop+높이)을 측정해 캔버스 최소 높이에 더한다.
  useLayoutEffect(() => {
    const el = laneRef.current;
    if (!el) {
      setLaneBottom(0);
      return;
    }
    const update = () => setLaneBottom(el.offsetTop + el.offsetHeight + CANVAS_PAD);
    update();
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, [showLanes, poolKey]);

  const hasContent = servers.length > 0 || accounts.length > 0;
  const selectedEdge = aEdges.find((e) => e.id === selected);
  const selectedAssignment = assignments.find((a) => a.id === selected);

  return (
    <div className={`topo-canvas${showLanes ? ' topo-has-lanes' : ''}`} onPointerDown={() => setSelected(null)}>
      <StatBar
        onlineServers={servers.filter((s) => s.status === 'online').length}
        totalServers={servers.length}
        activeAssignments={assignments.filter((a) => a.state === 'active').length}
        switchesLastHour={countSwitchesLastHour(accounts)}
        openAlerts={alertsData?.items?.length ?? 0}
        onGo={onGo}
      />

      {!hasContent && (
        <p className="topo-empty">서버·계정이 없습니다. 서버를 등록하고 계정을 추가하면 여기서 연결을 편집할 수 있습니다.</p>
      )}

      {hasContent && freeMode && (
        <div className="topo-toolbar">
          <span className="topo-toolbar-hint">노드를 끌어 배치 · 24px 격자 스냅</span>
          <button type="button" className="topo-reset" title="자동 배치로 되돌립니다" onClick={resetLayout}>정렬 초기화</button>
        </div>
      )}

      {hasContent && (
        <div
          className={`${freeMode ? 'topo-free' : 'topo-grid3'} ${dragFrom ? `drag-from-${dragFrom.from}` : ''}`.trim()}
          ref={gridRef}
        >
          <svg
            className="topo-edges"
            width={size.w || undefined}
            height={size.h || undefined}
            viewBox={size.w ? `0 0 ${size.w} ${size.h}` : undefined}
          >
            {mEdges.map((e) => (
              <path key={`m-${e.id}`} className="topo-medge" d={e.d} />
            ))}
            {aEdges.map((e) => (
              <g key={e.id} className={`topo-aedge ${selected === e.id ? 'sel' : ''}`.trim()}>
                <path className={`topo-edge ${e.kind}`.trim()} d={e.d} />
                {e.kind === 'active' && <path className="topo-flow" d={e.d} />}
                <path
                  className="topo-hit"
                  d={e.d}
                  onPointerDown={(ev) => { ev.stopPropagation(); setSelected(e.id); }}
                />
              </g>
            ))}
            {ghost && (
              <path
                className="topo-ghost"
                d={`M${ghost.x0},${ghost.y0} C${ghost.x0 + Math.max(40, Math.abs(ghost.x - ghost.x0) * 0.4)},${ghost.y0} ${ghost.x - Math.max(40, Math.abs(ghost.x - ghost.x0) * 0.4)},${ghost.y} ${ghost.x},${ghost.y}`}
              />
            )}
          </svg>

          <div className="topo-col topo-tenants">
            <div className="topo-col-head">테넌트</div>
            <TenantNode name={tenantName} serverCount={servers.length} nodeRef={setNode('tenant')} />
          </div>

          {freeMode ? (
            // 자유 배치: 서버·계정 노드를 캔버스에 absolute로 배치. 좌표는 JS 상태 소유.
            <div
              className="topo-canvas-area"
              ref={canvasRef}
              style={{
                minHeight: Math.max(canvasH, showLanes ? laneBottom : 0) || undefined,
                minWidth: showLanes ? POOL_RIGHT : undefined,
              }}
            >
              <div className="topo-band-label" style={{ left: SERVER_X }}>서버 <span className="topo-col-count">{orderedServers.length}</span></div>
              <div className="topo-band-label" style={{ left: ACCOUNT_X }}>계정 <span className="topo-col-count">{orderedAccounts.length}</span></div>
              {showLanes && poolLanes && (
                <div className="topo-pool-lanes" ref={laneRef} style={{ left: POOL_X, top: LANE_TOP }}>
                  <PoolLanes cooling={poolLanes.cooling} ready={poolLanes.ready} laneWidth={LANE_W} />
                </div>
              )}
              {orderedServers.map((s) => {
                const key = `srv:${s.id}`;
                const p = effectivePos(key);
                const activeId = activeByServer.get(s.id);
                // 호버 상세 카드가 캔버스 하단(overflow:hidden)에 잘리면 위로 뒤집는다.
                const h = nodeSize.current.get(key)?.h ?? DEF_SIZE.srv.h;
                const flip = canvasH > 0 && p.y + h + 168 > canvasH;
                return (
                  <div
                    key={s.id}
                    className={`topo-place srv ${flip ? 'flip' : ''} ${dragging?.key === key ? 'dragging' : ''}`.trim().replace(/\s+/g, ' ')}
                    style={{ left: p.x, top: p.y, width: NODE_W }}
                    onPointerDown={startNodeDrag('srv', s.id)}
                  >
                    <ServerNode
                      tenantId={tenantId}
                      server={s}
                      activeEmail={activeId ? emailOf.get(activeId) : undefined}
                      nodeRef={setNode(key)}
                      onClick={guardClick(() => onGo('servers'))}
                      onPortDown={startDrag('server', s.id)}
                    />
                  </div>
                );
              })}
              {orderedAccounts.map((a) => {
                const key = `acc:${a.id}`;
                const p = effectivePos(key);
                return (
                  <div
                    key={a.id}
                    className={`topo-place acc ${dragging?.key === key ? 'dragging' : ''}`.trim()}
                    style={{ left: p.x, top: p.y, width: NODE_W }}
                    onPointerDown={startNodeDrag('acc', a.id)}
                  >
                    <AccountNode
                      id={a.id}
                      email={a.email}
                      status={a.status}
                      provider={a.provider}
                      usagePct={usagePctByEmail.get(a.email)}
                      nodeRef={setNode(key)}
                      onClick={guardClick(() => onGo('accounts'))}
                      onPortDown={startDrag('account', a.id)}
                    />
                  </div>
                );
              })}
            </div>
          ) : (
            <>
              <div className="topo-col topo-servers">
                <div className="topo-col-head">서버 <span className="topo-col-count">{orderedServers.length}</span></div>
                <div className="topo-srv-grid">
                  {orderedServers.map((s) => {
                    const activeId = activeByServer.get(s.id);
                    return (
                      <ServerNode
                        key={s.id}
                        tenantId={tenantId}
                        server={s}
                        activeEmail={activeId ? emailOf.get(activeId) : undefined}
                        nodeRef={setNode(`srv:${s.id}`)}
                        onClick={() => onGo('servers')}
                        onPortDown={startDrag('server', s.id)}
                      />
                    );
                  })}
                  {orderedServers.length === 0 && <div className="topo-col-empty">서버 없음</div>}
                </div>
              </div>

              <div className="topo-col topo-accounts">
                <div className="topo-col-head">계정 <span className="topo-col-count">{orderedAccounts.length}</span></div>
                <div className="topo-acc-grid">
                  {orderedAccounts.map((a) => (
                    <AccountNode
                      key={a.id}
                      id={a.id}
                      email={a.email}
                      status={a.status}
                      provider={a.provider}
                      usagePct={usagePctByEmail.get(a.email)}
                      nodeRef={setNode(`acc:${a.id}`)}
                      onClick={() => onGo('accounts')}
                      onPortDown={startDrag('account', a.id)}
                    />
                  ))}
                  {orderedAccounts.length === 0 && <div className="topo-col-empty">계정 없음</div>}
                </div>
              </div>
            </>
          )}

          {selectedEdge && selectedAssignment && (
            <EdgePopover
              x={selectedEdge.mx}
              y={selectedEdge.my}
              assignment={selectedAssignment}
              srvName={serverName.get(selectedAssignment.serverId) ?? ''}
              email={emailOf.get(selectedAssignment.accountId) ?? ''}
              tenantId={tenantId}
              onBusy={setBusyEdge}
              onClose={() => setSelected(null)}
            />
          )}
        </div>
      )}

      {connect && (
        <ConnectModal
          tenantId={tenantId}
          accountId={connect.accountId}
          serverId={connect.serverId}
          email={emailOf.get(connect.accountId) ?? connect.accountId.slice(0, 8)}
          srvName={serverName.get(connect.serverId) ?? connect.serverId.slice(0, 8)}
          blocking={assignments.find(
            (a) => a.accountId === connect.accountId && a.state !== 'detached' && a.serverId !== connect.serverId,
          )}
          // 서버당 Codex 1계정(assignment.server_codex_capacity). 최종 판정은
          // 서버가 하지만, 확실히 막힐 연결은 눌러보기 전에 알려준다.
          codexBlockerEmail={
            providerOf.get(connect.accountId) === 'codex'
              ? emailOf.get(
                  assignments.find(
                    (a) =>
                      a.serverId === connect.serverId &&
                      a.state !== 'detached' &&
                      a.accountId !== connect.accountId &&
                      providerOf.get(a.accountId) === 'codex',
                  )?.accountId ?? '',
                )
              : undefined
          }
          srvNameOf={serverName}
          onClose={() => setConnect(null)}
        />
      )}

      {excludedNotice && (
        <Modal title="배정 제외" onClose={() => setExcludedNotice(null)}>
          <p>{excludedNotice}</p>
        </Modal>
      )}
    </div>
  );
}

// 선 중앙 액션 팝오버 — 상태별 허용 verb만 노출(AssignmentsPanel과 동일 규칙).
// 낙관적 갱신 없이 실행 후 SWR 재검증으로 수렴한다. 실행 중에는 onBusy로 해당
// 선을 "동기화 중" 점멸시킨다.
function EdgePopover({
  x,
  y,
  assignment,
  srvName,
  email,
  tenantId,
  onBusy,
  onClose,
}: {
  x: number;
  y: number;
  assignment: Assignment;
  srvName: string;
  email: string;
  tenantId: string;
  onBusy: (id: string | null) => void;
  onClose: () => void;
}) {
  const { mutate } = useSWRConfig();
  const act = useAction();
  const allowed = allowedAssignmentActions(assignment.state);

  function run(verb: Verb) {
    onBusy(assignment.id);
    // 성공/실패 공통 정리(finally)로 선 점멸을 반드시 해제한다. onDone은 성공
    // 시에만 실행되므로 여기서 리셋하면 실패 시 선이 영구 점멸하는 누수가 없다.
    act
      .run(
        () => api.assignmentAction(tenantId, assignment.id, verb),
        async () => {
          await mutate(['assignments', tenantId]);
          if (verb === 'switch-now') mutate(['accounts', tenantId]);
          onClose();
        },
      )
      .finally(() => onBusy(null));
  }

  return (
    <div
      className="topo-popover"
      style={{ left: x, top: y }}
      onPointerDown={(e) => e.stopPropagation()}
    >
      <div className="topo-popover-head">
        <span className="mono">{email}</span> → {srvName}
      </div>
      {act.error && <div className="topo-popover-err">{act.error}</div>}
      <div className="topo-popover-actions">
        {allowed.length === 0 && <span className="topo-popover-none">가능한 동작 없음</span>}
        {allowed.map((v) => (
          <button
            key={v}
            className={`vbtn ${VERB_STYLE[v] ?? ''}`.trim()}
            disabled={act.busy}
            onClick={() => run(v)}
          >
            <span className="vbtn-icon"><Icon name={VERB_ICON[v]} size={14} /></span>
            {VERB_LABEL[v]}
          </button>
        ))}
      </div>
    </div>
  );
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
// A→B 이동 체인 폴링: recall 요청 후 기존 할당이 detached로 수렴할 때까지 대기.
const MOVE_DEADLINE_MS = 30_000;
const MOVE_POLL_MS = 1_500;

// 연결 확인 모달 — 기존 API만 사용한다. P1 백엔드는 deliverImmediately를 400으로
// 거부하고 계정당 비-detached 할당을 유일하게(uq_assignments_active_account) 강제하므로:
//  - 다른 서버에 비-detached 할당이 있으면(blocking) A에서 회수 후 이 서버로 옮기는
//    이동 체인을 제공한다: recall → 기존 할당이 detached로 수렴할 때까지 폴링(상한
//    30s) → createAssignment → deliver. 각 단계 실패는 어디까지 진행됐는지 구분해
//    표출하고(낙관적 갱신 없이 서버 상태로 수렴), 폴링 타임아웃은 재시도를 안내한다.
//  - 아니면 표 패널과 동일하게 할당을 생성(전달 옵션 없이)한 뒤 deliver verb를 체인.
//    체인 실패 시 할당은 남으므로 "생성됨, 전달 실패"로 구분 표출한다.
function ConnectModal({
  tenantId,
  accountId,
  serverId,
  email,
  srvName,
  blocking,
  codexBlockerEmail,
  srvNameOf,
  onClose,
}: {
  tenantId: string;
  accountId: string;
  serverId: string;
  email: string;
  srvName: string;
  blocking?: Assignment;
  codexBlockerEmail?: string;
  srvNameOf: Map<string, string>;
  onClose: () => void;
}) {
  const { mutate } = useSWRConfig();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [step, setStep] = useState('');
  // 모달이 닫히거나 언마운트되면 진행 중인 이동 체인을 중단시킨다(고아 체인·409 경합 방지).
  const cancelled = useRef(false);
  useEffect(() => () => { cancelled.current = true; }, []);
  const fromName = blocking ? srvNameOf.get(blocking.serverId) ?? '다른 서버' : '';

  // busy 중에는 닫기를 막아 진행 중인 체인이 UI 없이 계속 돌지 않게 한다.
  function guardedClose() {
    if (busy) return;
    onClose();
  }

  async function revalidate() {
    await mutate(['assignments', tenantId]);
    mutate(['servers', tenantId]);
    mutate(['accounts', tenantId]);
  }

  // A→B 이동 — blocking(다른 서버의 비-detached 할당)을 회수한 뒤 이 서버로 재연결한다.
  // 낙관적 갱신 없이 매 폴링마다 assignments를 재검증해 실제 detached 수렴을 확인한다.
  async function move() {
    if (!blocking) return;
    setBusy(true);
    setError('');
    setStep(`${fromName}에서 회수 중…`);
    try {
      await api.assignmentAction(tenantId, blocking.id, 'recall');
    } catch (e) {
      if (cancelled.current) return;
      await revalidate();
      setStep('');
      setError(`회수 실패: ${krApiError(e)}`);
      setBusy(false);
      return;
    }
    if (cancelled.current) return;
    // 기존 할당이 detached로 수렴할 때까지 폴링(상한 30s). 폴링 중 일시적 조회
    // 실패는 스킵하고 다음 tick에 재시도하며, 계속 실패하면 타임아웃 경로로 떨어진다.
    const start = Date.now();
    let detached = false;
    while (!cancelled.current && Date.now() - start < MOVE_DEADLINE_MS) {
      await sleep(MOVE_POLL_MS);
      if (cancelled.current) return;
      try {
        const page = (await mutate(['assignments', tenantId])) as AssignmentPage | undefined;
        const row = page?.items.find((a) => a.id === blocking.id);
        if (row && row.state === 'detached') { detached = true; break; }
      } catch {
        // 일시 오류 — 다음 tick 재시도.
      }
    }
    if (cancelled.current) return;
    if (!detached) {
      await revalidate();
      setStep('');
      setError('회수는 요청됨 — 잠시 후 다시 드래그해 이동을 완료하세요.');
      setBusy(false);
      return;
    }
    setStep(`${srvName}에 재연결 중…`);
    let created: Assignment;
    try {
      created = await api.createAssignment(tenantId, { accountId, serverId });
    } catch (e) {
      if (cancelled.current) return;
      await revalidate();
      setStep('');
      setError(`회수됨, 재연결 실패: ${krApiError(e)}. 이 계정을 다시 드래그하면 연결부터 재시도합니다.`);
      setBusy(false);
      return;
    }
    if (cancelled.current) return;
    setStep('전달 중…');
    try {
      await api.assignmentAction(tenantId, created.id, 'deliver');
    } catch (e) {
      if (cancelled.current) return;
      await revalidate();
      setStep('');
      setError(`회수·재연결됨, 전달 실패: ${krApiError(e)}`);
      setBusy(false);
      return;
    }
    if (cancelled.current) return;
    await revalidate();
    setStep('');
    setBusy(false);
    onClose();
  }

  async function confirm() {
    setBusy(true);
    setError('');
    let created: Assignment;
    try {
      created = await api.createAssignment(tenantId, { accountId, serverId });
    } catch (e) {
      setError(`할당 생성 실패: ${krApiError(e)}`);
      setBusy(false);
      return;
    }
    try {
      await api.assignmentAction(tenantId, created.id, 'deliver');
    } catch (e) {
      await revalidate();
      setError(`할당은 생성됨, 전달 실패: ${krApiError(e)}`);
      setBusy(false);
      return;
    }
    await revalidate();
    setBusy(false);
    onClose();
  }

  return (
    <Modal title="계정 연결" onClose={guardedClose}>
      <p>
        <span className="mono">{email}</span> 계정을 <b>{srvName}</b> 서버에 연결(할당 생성 후 전달)합니다.
      </p>
      {blocking ? (
        <>
          <p className="topo-move-note">
            이 계정은 <b>{fromName}</b>에 {krLabel(blocking.state)} 상태의 할당이 있습니다. 계정당 하나만
            연결할 수 있으므로, <b>{fromName}</b>에서 회수한 뒤 이 서버로 이동합니다.
          </p>
          {codexBlockerEmail && (
            <p className="topo-move-note">
              이 서버에는 이미 Codex 계정 <span className="mono">{codexBlockerEmail}</span>이(가) 연결돼
              있습니다. Codex는 호스트당 자격증명을 하나만 두므로 이동해도 서버가 거부할 수 있습니다.
            </p>
          )}
          <button className="primary" style={{ marginTop: 14 }} disabled={busy} onClick={move}>
            {fromName}에서 회수 후 이 서버로 이동
          </button>
        </>
      ) : (
        <>
          {codexBlockerEmail && (
            <p className="topo-move-note">
              이 서버에는 이미 Codex 계정 <span className="mono">{codexBlockerEmail}</span>이(가) 연결돼
              있습니다. Codex는 호스트당 자격증명을 하나만 두므로 이대로 진행하면 서버가 거부합니다.
              기존 연결을 먼저 회수하세요.
            </p>
          )}
          <button className="primary" style={{ marginTop: 14 }} disabled={busy} onClick={confirm}>
            연결 생성
          </button>
        </>
      )}
      {/* 진행 단계·오류는 blocking 여부와 무관하게 공통 영역에서 표시(폴링 중 분기 전환에도 유지). */}
      {step && <p className="muted" style={{ marginTop: 10 }}>{step}</p>}
      {error && <p className="err" style={{ marginTop: 10 }}>{error}</p>}
    </Modal>
  );
}

// 최근 1시간 내 lastSwitchedAt을 가진 계정 수.
function countSwitchesLastHour(accounts: { lastSwitchedAt?: string }[]): number {
  const cutoff = Date.now() - 3600_000;
  let n = 0;
  for (const a of accounts) {
    if (!a.lastSwitchedAt) continue;
    const t = new Date(a.lastSwitchedAt).getTime();
    if (!Number.isNaN(t) && t >= cutoff) n++;
  }
  return n;
}

// measure 재실행 의존키 — 할당 구성·상태·서버 순서가 바뀔 때만 갱신.
function layoutKey(
  specs: { id: string; accId: string; srvId: string; kind: EdgeKind }[],
  serverIds: string[],
): string {
  return serverIds.join(',') + '#' + specs.map((s) => `${s.id}:${s.accId}:${s.srvId}:${s.kind}`).join('|');
}
