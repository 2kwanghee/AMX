'use client';

import type { PointerEvent as ReactPointerEvent } from 'react';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import useSWR, { useSWRConfig } from 'swr';
import { allowedAssignmentActions, api, krApiError } from '@/lib/api-client/client';
import type { AssignmentActionVerb as Verb } from '@/lib/api-client/client';
import type {
  Account,
  AccountPage,
  AlertPage,
  Assignment,
  AssignmentPage,
  EnrollTokenResponse,
  PoolOverview,
  Server,
  ServerPage,
  TenantPage,
} from '@/lib/api-client/types';
import { accountWindows } from '@/lib/usage-format';
import { groupAccountsByLane } from '@/lib/pool';
import { DirectImport } from '../accounts/DirectImportModal';
import { EditAccount } from '../accounts/EditAccountModal';
import { RegisterModal } from '../accounts/RegisterModal';
import { currentActiveByServer } from '../AssignmentsPanel';
import { Icon, krLabel, markDataArrived, Modal, useAction } from '../common';
import { CreateServer } from '../servers/CreateServerModal';
import { EnrollTokenModal } from '../servers/EnrollTokenModal';
import { EventsModal } from '../servers/EventsModal';
import { PolicyModal } from '../servers/PolicyModal';
import { SelfUpdateModal } from '../servers/SelfUpdateModal';
import { UsageModal } from '../servers/UsageModal';
import { AccountNode } from './AccountNode';
import { PoolLanes } from './PoolLaneChip';
import { ServerNode } from './ServerNode';
import { StatBar } from './StatBar';
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
// 노드 액션 팝오버 앵커. flip=true면 노드 상단 위로 펼친다(캔버스 하단 clip 회피).
type NodeAnchor = { x: number; y: number; flip: boolean };

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
// 노드 액션 팝오버 예상 높이(px) — 캔버스 하단 clip 판정용 상한 추정치.
// 서버는 버튼 8개(줄바꿈 포함, ~180~200px), 계정은 버튼 2개.
const POPOVER_H: Record<'srv' | 'acc', number> = { srv: 210, acc: 90 };

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
export function TopologyView({ tenantId }: { tenantId: string }) {
  const { data: tenantsData } = useSWR<TenantPage>('tenants', () => api.listTenants());
  const { data: serversData, mutate: mutateServers } = useSWR<ServerPage>(['servers', tenantId], () => api.listServers(tenantId), {
    refreshInterval: 7000,
    onSuccess: () => markDataArrived(),
  });
  const { data: accountsData, mutate: mutateAccounts } = useSWR<AccountPage>(['accounts', tenantId], () => api.listAccounts(tenantId), {
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

  // -- 콘솔 통합 액션(서버·계정 노드 클릭) -----------------------------------
  // 메뉴 이동 없이 노드 클릭 → 액션 팝오버 → (필요 시) 패널과 같은 추출 모달.
  // 위치는 nodeAnchors(측정된 노드 하단/상단 앵커 + flip)를 그대로 쓴다.
  const [srvActionId, setSrvActionId] = useState<string | null>(null);
  const [accActionId, setAccActionId] = useState<string | null>(null);
  const [usageOf, setUsageOf] = useState<Server | null>(null);
  const [eventsOf, setEventsOf] = useState<Server | null>(null);
  const [policyOf, setPolicyOf] = useState<Server | null>(null);
  const [tokenOf, setTokenOf] = useState<EnrollTokenResponse | null>(null);
  const [updateOf, setUpdateOf] = useState<Server | null>(null);
  const [editingAccount, setEditingAccount] = useState<Account | null>(null);
  const [creatingServer, setCreatingServer] = useState(false);
  const [creatingAccount, setCreatingAccount] = useState(false);
  const [creatingDirectImport, setCreatingDirectImport] = useState(false);
  const [nodeAnchors, setNodeAnchors] = useState<Record<string, NodeAnchor>>({});

  // 노드 pointerdown이 캔버스까지 버블돼(아래 onPointerDown) 클릭보다 먼저 상태를
  // 지운다 — 그 지우기 직전 값을 여기 담아 뒀다가 click 시점에 "같은 노드 재클릭"을
  // 판별해 토글 닫힘을 만든다.
  const lastSrvActionRef = useRef<string | null>(null);
  const lastAccActionRef = useRef<string | null>(null);

  function openSrvAction(id: string) {
    setSelected(null);
    setAccActionId(null);
    const wasOpen = lastSrvActionRef.current === id;
    lastSrvActionRef.current = null; // 비교 즉시 소비 — 다음 상관없는 재클릭에 남지 않게.
    setSrvActionId(wasOpen ? null : id);
  }
  function openAccAction(id: string) {
    setSelected(null);
    setSrvActionId(null);
    const wasOpen = lastAccActionRef.current === id;
    lastAccActionRef.current = null;
    setAccActionId(wasOpen ? null : id);
  }
  function closeNodeActions() {
    lastSrvActionRef.current = srvActionId;
    lastAccActionRef.current = accActionId;
    setSrvActionId(null);
    setAccActionId(null);
  }

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
    // 노드 액션 팝오버 앵커. 아래 공간이 부족해도 위 공간까지 부족하면(캔버스 상단
    // 가까이 있는 노드) 무조건 뒤집지 않는다 — "위 공간이 POPOVER_H 이상 확보될
    // 때만" 뒤집어야 뒤집힌 팝오버가 캔버스 위로 잘리는 걸 막는다. 자유 배치는
    // canvasH(=bottom, 방금 계산)를, 그리드 폴백은 실측 컨테이너 높이를 하단
    // 경계로 쓴다 — 이 시점엔 아직 size state가 이전 렌더 값이라(이번 measure의
    // 결과가 반영되기 전) grid.offsetHeight를 직접 재서 쓴다(값 자체는 이번
    // 사이클이 끝나면 size.h가 되는 것과 같다). 위·아래 모두 부족하면 뒤집지
    // 않고 아래로 연다 — 그리드 폴백은 노드 자체가 일반 흐름이라 컨테이너가
    // 대개 콘텐츠를 따라 늘어나 있어 이 낙폭이 실제로는 드물다(자유 배치는
    // openPopoverBottom으로 캔버스를 늘려 커버한다).
    const boundBottom = wide ? bottom : grid.offsetHeight;
    const nextAnchors: Record<string, NodeAnchor> = {};
    for (const s of orderedServers) {
      const el = nodeRefs.current.get(`srv:${s.id}`);
      if (!el) continue;
      const r = el.getBoundingClientRect();
      const belowY = r.bottom - base.top + 10;
      const topY = r.top - base.top - 10;
      const belowInsufficient = boundBottom > 0 && belowY + POPOVER_H.srv > boundBottom;
      const flip = belowInsufficient && topY >= POPOVER_H.srv;
      nextAnchors[`srv:${s.id}`] = { x: r.left - base.left + r.width / 2, y: flip ? topY : belowY, flip };
    }
    for (const a of orderedAccounts) {
      const el = nodeRefs.current.get(`acc:${a.id}`);
      if (!el) continue;
      const r = el.getBoundingClientRect();
      const belowY = r.bottom - base.top + 10;
      const topY = r.top - base.top - 10;
      const belowInsufficient = boundBottom > 0 && belowY + POPOVER_H.acc > boundBottom;
      const flip = belowInsufficient && topY >= POPOVER_H.acc;
      nextAnchors[`acc:${a.id}`] = { x: r.left - base.left + r.width / 2, y: flip ? topY : belowY, flip };
    }
    setNodeAnchors(nextAnchors);
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
  const srvActionServer = srvActionId ? servers.find((s) => s.id === srvActionId) : undefined;
  const srvActionPos = srvActionId ? nodeAnchors[`srv:${srvActionId}`] : undefined;
  const accActionAccount = accActionId ? accounts.find((a) => a.id === accActionId) : undefined;
  const accActionPos = accActionId ? nodeAnchors[`acc:${accActionId}`] : undefined;
  // 열린 노드 팝오버가 아래로 펼쳐질 때(비-flip) 그 하단 소요를 자유 배치 캔버스
  // minHeight에 반영한다 — 위·아래 모두 부족해 flip 못 하고 아래로 연 경우에도
  // 캔버스 자체가 늘어나 잘리지 않게 한다(그리드 폴백은 이 minHeight 메커니즘이
  // 없어 대상 아님).
  const openPopoverBottom = Math.max(
    srvActionPos && !srvActionPos.flip ? srvActionPos.y + POPOVER_H.srv + CANVAS_PAD : 0,
    accActionPos && !accActionPos.flip ? accActionPos.y + POPOVER_H.acc + CANVAS_PAD : 0,
  );

  return (
    <div
      className={`topo-canvas${showLanes ? ' topo-has-lanes' : ''}`}
      onPointerDown={() => { setSelected(null); closeNodeActions(); }}
    >
      <StatBar
        onlineServers={servers.filter((s) => s.status === 'online').length}
        totalServers={servers.length}
        activeAssignments={assignments.filter((a) => a.state === 'active').length}
        switchesLastHour={countSwitchesLastHour(accounts)}
        openAlerts={alertsData?.items?.length ?? 0}
      />

      {/* 등록 버튼은 콘텐츠 유무와 무관하게 항상 노출한다 — 서버·계정이 하나도
          없는 신규 테넌트도 여기서 첫 등록을 끝낼 수 있어야 한다. */}
      <div className="topo-toolbar">
        {hasContent && freeMode && (
          <span className="topo-toolbar-hint">노드를 끌어 배치 · 24px 격자 스냅</span>
        )}
        <button type="button" className="topo-reset primary" onClick={() => setCreatingServer(true)}>서버 등록</button>
        <button type="button" className="topo-reset primary" onClick={() => setCreatingAccount(true)}>계정 등록</button>
        <button type="button" className="topo-reset" onClick={() => setCreatingDirectImport(true)}>API 키 가져오기</button>
        {hasContent && freeMode && (
          <button type="button" className="topo-reset" title="자동 배치로 되돌립니다" onClick={resetLayout}>정렬 초기화</button>
        )}
      </div>

      {!hasContent && (
        <p className="topo-empty">서버·계정이 없습니다. 위 &apos;서버 등록&apos;·&apos;계정 등록&apos;으로 시작하세요.</p>
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
                  onPointerDown={(ev) => { ev.stopPropagation(); setSelected(e.id); closeNodeActions(); }}
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
                minHeight: Math.max(canvasH, showLanes ? laneBottom : 0, openPopoverBottom) || undefined,
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
                      onClick={guardClick(() => openSrvAction(s.id))}
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
                      onClick={guardClick(() => openAccAction(a.id))}
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
                        onClick={guardClick(() => openSrvAction(s.id))}
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
                      onClick={guardClick(() => openAccAction(a.id))}
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

          {srvActionServer && srvActionPos && (
            <ServerActionPopover
              tenantId={tenantId}
              server={srvActionServer}
              x={srvActionPos.x}
              y={srvActionPos.y}
              flip={srvActionPos.flip}
              onClose={closeNodeActions}
              onMutate={() => mutateServers()}
              onUsage={() => setUsageOf(srvActionServer)}
              onEvents={() => setEventsOf(srvActionServer)}
              onPolicy={() => setPolicyOf(srvActionServer)}
              onToken={(t) => setTokenOf(t)}
              onUpdate={() => setUpdateOf(srvActionServer)}
            />
          )}

          {accActionAccount && accActionPos && (
            <AccountActionPopover
              tenantId={tenantId}
              account={accActionAccount}
              x={accActionPos.x}
              y={accActionPos.y}
              flip={accActionPos.flip}
              onClose={closeNodeActions}
              onMutate={() => mutateAccounts()}
              onEdit={() => setEditingAccount(accActionAccount)}
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

      {/* 서버 액션 팝오버가 여는 모달 — 서버·계정 패널과 동일한 추출 컴포넌트를
          그대로 재사용한다(동작 동일, 진입점만 상황판). */}
      {creatingServer && (
        <CreateServer
          tenantId={tenantId}
          onClose={() => setCreatingServer(false)}
          onDone={() => { setCreatingServer(false); mutateServers(); }}
        />
      )}
      {usageOf && <UsageModal tenantId={tenantId} server={usageOf} onClose={() => setUsageOf(null)} />}
      {eventsOf && <EventsModal tenantId={tenantId} server={eventsOf} onClose={() => setEventsOf(null)} />}
      {policyOf && (
        <PolicyModal
          tenantId={tenantId}
          server={policyOf}
          onClose={() => setPolicyOf(null)}
          onDone={() => { setPolicyOf(null); mutateServers(); }}
        />
      )}
      {updateOf && (
        <SelfUpdateModal
          tenantId={tenantId}
          server={servers.find((x) => x.id === updateOf.id) ?? updateOf}
          onClose={() => setUpdateOf(null)}
        />
      )}
      {tokenOf && <EnrollTokenModal token={tokenOf} onClose={() => setTokenOf(null)} />}

      {creatingAccount && (
        <RegisterModal
          tenantId={tenantId}
          onClose={() => setCreatingAccount(false)}
          onDone={() => { setCreatingAccount(false); mutateAccounts(); }}
        />
      )}
      {creatingDirectImport && (
        <DirectImport
          tenantId={tenantId}
          onClose={() => setCreatingDirectImport(false)}
          onDone={() => { setCreatingDirectImport(false); mutateAccounts(); }}
        />
      )}
      {editingAccount && (
        <EditAccount
          tenantId={tenantId}
          account={editingAccount}
          onClose={() => setEditingAccount(null)}
          onDone={() => { setEditingAccount(null); mutateAccounts(); }}
        />
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

// 노드 액션 팝오버 공통 키보드 처리 — 열릴 때 첫 버튼 포커스, Escape로 닫기.
// EdgePopover에는 아직 이런 장치가 없어(선 팝오버는 이번 변경 범위 밖) 노드
// 팝오버에만 최소로 둔다.
function useNodePopoverKeys(rootRef: { current: HTMLDivElement | null }, onClose: () => void) {
  useEffect(() => {
    (rootRef.current?.querySelector('button') as HTMLButtonElement | null)?.focus();
    function onKey(ev: KeyboardEvent) {
      if (ev.key === 'Escape') onClose();
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}

// 서버 노드 액션 팝오버 — 서버 패널 타일의 동작 전부(전환 모드·사용량 갱신·사용량·
// 이벤트·정책·등록 토큰·에이전트 업데이트·삭제)를 메뉴 이동 없이 여기서 낸다.
// 모달을 여는 버튼은 팝오버를 먼저 닫는다(모달이 화면을 덮으므로). 전환 모드·
// 삭제는 성공 시 목록을 재검증(onMutate)하고 팝오버를 닫는다. 사용량 갱신은
// 원래 패널 타일과 동일하게 결과를 기다리지 않는 발사 후 잊는 동작이라 팝오버를 유지한다.
// 삭제는 2단계 확인(첫 클릭에 "정말 삭제"로 강조, 두 번째 클릭에 실행) — 팝오버가
// 닫히면 이 컴포넌트가 언마운트되므로 confirmDelete 상태는 자연히 리셋된다.
function ServerActionPopover({
  tenantId,
  server,
  x,
  y,
  flip,
  onClose,
  onMutate,
  onUsage,
  onEvents,
  onPolicy,
  onToken,
  onUpdate,
}: {
  tenantId: string;
  server: Server;
  x: number;
  y: number;
  flip: boolean;
  onClose: () => void;
  onMutate: () => void;
  onUsage: () => void;
  onEvents: () => void;
  onPolicy: () => void;
  onToken: (t: EnrollTokenResponse) => void;
  onUpdate: () => void;
}) {
  const act = useAction();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  useNodePopoverKeys(rootRef, onClose);

  function toggleMode() {
    act.run(
      () => api.setSwitchMode(tenantId, server.id, server.switchMode === 'auto' ? 'manual' : 'auto'),
      () => { onMutate(); onClose(); },
    );
  }

  function issueToken() {
    act.run(async () => {
      const t = await api.issueEnrollToken(tenantId, server.id);
      onToken(t);
      onClose();
    });
  }

  function remove() {
    if (!confirmDelete) { setConfirmDelete(true); return; }
    act.run(() => api.deleteServer(tenantId, server.id), () => { onMutate(); onClose(); });
  }

  return (
    <div
      ref={rootRef}
      className={`topo-popover node${flip ? ' flip' : ''}`}
      style={{ left: x, top: y }}
      onPointerDown={(e) => e.stopPropagation()}
    >
      <div className="topo-popover-head">{server.name}</div>
      {act.error && <div className="topo-popover-err">{act.error}</div>}
      <div className="topo-popover-actions">
        <button className="vbtn" disabled={act.busy} onClick={toggleMode}>
          <span className="vbtn-icon"><Icon name={server.switchMode === 'auto' ? 'hand' : 'zap'} size={14} /></span>
          {server.switchMode === 'auto' ? '수동 전환으로' : '자동 전환으로'}
        </button>
        <button className="vbtn" disabled={act.busy} onClick={() => act.run(() => api.refreshUsage(tenantId, server.id))}>
          <span className="vbtn-icon"><Icon name="refresh" size={14} /></span>
          사용량 갱신
        </button>
        <button className="vbtn" onClick={() => { onUsage(); onClose(); }}>
          <span className="vbtn-icon"><Icon name="gauge" size={14} /></span>
          사용량
        </button>
        <button className="vbtn" onClick={() => { onEvents(); onClose(); }}>
          <span className="vbtn-icon"><Icon name="activity" size={14} /></span>
          이벤트
        </button>
        <button className="vbtn" onClick={() => { onPolicy(); onClose(); }}>
          <span className="vbtn-icon"><Icon name="sliders" size={14} /></span>
          정책
        </button>
        <button className="vbtn" disabled={act.busy} onClick={issueToken}>
          <span className="vbtn-icon"><Icon name="key" size={14} /></span>
          등록 토큰
        </button>
        <button className="vbtn" onClick={() => { onUpdate(); onClose(); }}>
          <span className="vbtn-icon"><Icon name="rotate" size={14} /></span>
          에이전트 업데이트
        </button>
        <button className={`vbtn danger${confirmDelete ? ' confirm' : ''}`} disabled={act.busy} onClick={remove}>
          <span className="vbtn-icon"><Icon name="trash" size={14} /></span>
          {confirmDelete ? '정말 삭제' : '삭제'}
        </button>
      </div>
    </div>
  );
}

// 계정 노드 액션 팝오버 — 계정 패널 행의 동작(수정·삭제)을 그대로 낸다. 삭제는
// 서버 팝오버와 같은 2단계 확인으로 바꾼다(패널 자체의 즉시 삭제 동작은 그대로 둔다).
function AccountActionPopover({
  tenantId,
  account,
  x,
  y,
  flip,
  onClose,
  onMutate,
  onEdit,
}: {
  tenantId: string;
  account: Account;
  x: number;
  y: number;
  flip: boolean;
  onClose: () => void;
  onMutate: () => void;
  onEdit: () => void;
}) {
  const act = useAction();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  useNodePopoverKeys(rootRef, onClose);

  function remove() {
    if (!confirmDelete) { setConfirmDelete(true); return; }
    act.run(() => api.deleteAccount(tenantId, account.id), () => { onMutate(); onClose(); });
  }

  return (
    <div
      ref={rootRef}
      className={`topo-popover node${flip ? ' flip' : ''}`}
      style={{ left: x, top: y }}
      onPointerDown={(e) => e.stopPropagation()}
    >
      <div className="topo-popover-head"><span className="mono">{account.email}</span></div>
      {act.error && <div className="topo-popover-err">{act.error}</div>}
      <div className="topo-popover-actions">
        <button className="vbtn" onClick={() => { onEdit(); onClose(); }}>
          <span className="vbtn-icon"><Icon name="user" size={14} /></span>
          수정
        </button>
        <button className={`vbtn danger${confirmDelete ? ' confirm' : ''}`} disabled={act.busy} onClick={remove}>
          <span className="vbtn-icon"><Icon name="trash" size={14} /></span>
          {confirmDelete ? '정말 삭제' : '삭제'}
        </button>
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
