'use client';

import type { PointerEvent as ReactPointerEvent } from 'react';
import { useCallback, useLayoutEffect, useRef, useState } from 'react';
import useSWR, { useSWRConfig } from 'swr';
import { allowedAssignmentActions, api, krApiError } from '@/lib/api-client/client';
import type { AssignmentActionVerb as Verb } from '@/lib/api-client/client';
import type {
  AccountPage,
  AlertPage,
  Assignment,
  AssignmentPage,
  ServerPage,
  TenantPage,
} from '@/lib/api-client/types';
import { currentActiveByServer } from '../AssignmentsPanel';
import { Icon, krLabel, markDataArrived, Modal, useAction } from '../common';
import { AccountNode } from './AccountNode';
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

  const minServerIdx = new Map<string, number>();
  for (const a of assignments) {
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
  const nodeRefs = useRef<Map<string, HTMLElement>>(new Map());
  const [aEdges, setAEdges] = useState<AEdge[]>([]);
  const [mEdges, setMEdges] = useState<MEdge[]>([]);
  const [size, setSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 });
  const [ghost, setGhost] = useState<Ghost | null>(null);
  const [dragFrom, setDragFrom] = useState<DragFrom>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [busyEdge, setBusyEdge] = useState<string | null>(null);
  const [connect, setConnect] = useState<{ accountId: string; serverId: string } | null>(null);

  const setNode = useCallback((id: string) => (el: HTMLElement | null) => {
    if (el) nodeRefs.current.set(id, el);
    else nodeRefs.current.delete(id);
  }, []);

  const edgeSpecs = assignments
    .filter((a) => serverIndex.has(a.serverId) && emailOf.has(a.accountId))
    .map((a) => {
      let kind: EdgeKind = '';
      if (a.lastError) kind = 'error';
      else if (a.pendingCommandId || busyEdge === a.id) kind = 'pending';
      else if (activeByServer.get(a.serverId) === a.accountId) kind = 'active';
      return { id: a.id, accId: a.accountId, srvId: a.serverId, kind };
    });

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
    setMEdges(nextM);
    setAEdges(nextA);
    setSize({ w: grid.offsetWidth, h: grid.offsetHeight });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layoutKey(edgeSpecs, orderedServers.map((s) => s.id))]);

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
    // 이미 같은 서버에 연결돼 있으면 무효(중복 생성 안 함).
    if (assignments.some((a) => a.accountId === accountId && a.serverId === serverId)) return;
    setConnect({ accountId, serverId });
  }

  const hasContent = servers.length > 0 || accounts.length > 0;
  const selectedEdge = aEdges.find((e) => e.id === selected);
  const selectedAssignment = assignments.find((a) => a.id === selected);

  return (
    <div className="topo-canvas" onPointerDown={() => setSelected(null)}>
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

      {hasContent && (
        <div
          className={`topo-grid3 ${dragFrom ? `drag-from-${dragFrom.from}` : ''}`.trim()}
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
                  nodeRef={setNode(`acc:${a.id}`)}
                  onClick={() => onGo('accounts')}
                  onPortDown={startDrag('account', a.id)}
                />
              ))}
              {orderedAccounts.length === 0 && <div className="topo-col-empty">계정 없음</div>}
            </div>
          </div>

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

// 연결 확인 모달 — 기존 API만 사용한다. P1 백엔드는 deliverImmediately를 400으로
// 거부하고 계정당 비-detached 할당을 유일하게(uq_assignments_active_account) 강제하므로:
//  - 다른 서버에 비-detached 할당이 있으면 생성 자체를 막고 회수를 안내(409 회피).
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
  const fromName = blocking ? srvNameOf.get(blocking.serverId) ?? '다른 서버' : '';

  async function revalidate() {
    await mutate(['assignments', tenantId]);
    mutate(['servers', tenantId]);
    mutate(['accounts', tenantId]);
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
    <Modal title="계정 연결" onClose={onClose}>
      <p>
        <span className="mono">{email}</span> 계정을 <b>{srvName}</b> 서버에 연결(할당 생성 후 전달)합니다.
      </p>
      {blocking ? (
        <p className="topo-move-note">
          이 계정은 <b>{fromName}</b>에 {krLabel(blocking.state)} 상태의 할당이 있습니다. 계정당 하나만
          연결할 수 있으니, 먼저 해당 할당을 회수한 뒤 다시 연결하세요.
        </p>
      ) : (
        <>
          {codexBlockerEmail && (
            <p className="topo-move-note">
              이 서버에는 이미 Codex 계정 <span className="mono">{codexBlockerEmail}</span>이(가) 연결돼
              있습니다. Codex는 호스트당 자격증명을 하나만 두므로 이대로 진행하면 서버가 거부합니다.
              기존 연결을 먼저 회수하세요.
            </p>
          )}
          {error && <p className="err">{error}</p>}
          <button className="primary" style={{ marginTop: 14 }} disabled={busy} onClick={confirm}>
            연결 생성
          </button>
        </>
      )}
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
