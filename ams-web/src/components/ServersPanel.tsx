'use client';

import { Fragment, useState } from 'react';
import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import type {
  AccountPage,
  AssignmentPage,
  EnrollTokenResponse,
  EventPage,
  SelfUpdateStatus,
  Server,
  ServerPage,
  ServerUpdate,
  SwitchStrategy,
  UsageSnapshot,
} from '@/lib/api-client/types';
import { formatEventRow } from '@/lib/event-format';
import { currentActiveByServer } from './AssignmentsPanel';
import {
  AvatarStack,
  Badge,
  CopyButton,
  Icon,
  LiveDot,
  Modal,
  SwitchModePill,
  TimeCell,
  fmtTime,
  krLabel,
  providerIcon,
  useAction,
  useMarkOnData,
  useNow,
} from './common';

const POLL = 7000;

// 하트비트 메트릭 표기 — 에이전트가 아직 보고 안 했으면(구버전 포함) null이다.
function fmtPct(n?: number | null) {
  return n === undefined || n === null ? '—' : `${Math.round(n)}%`;
}

// 마지막 접속 카운트업 표기. offline이면 crit 톤, 90초 초과면 warn + 경고 아이콘.
function lastSeen(iso: string | undefined, offline: boolean, now: number) {
  if (!iso) return { cls: offline ? 'crit' : '', warn: offline, text: '접속 기록 없음' };
  const secs = Math.max(0, Math.floor((now - new Date(iso).getTime()) / 1000));
  const dur = secs < 60 ? `${secs}초` : `${Math.floor(secs / 60)}분 ${secs % 60}초`;
  if (offline) return { cls: 'crit', warn: true, text: `오프라인 · 마지막 ${dur} 전` };
  if (secs > 90) return { cls: 'warn', warn: true, text: `마지막 접속 ${dur} 전` };
  return { cls: '', warn: false, text: `마지막 접속 ${secs}초 전` };
}

export function ServersPanel({ tenantId, variant = 'full' }: { tenantId: string; variant?: 'home' | 'full' }) {
  const { data, mutate } = useSWR<ServerPage>(
    ['servers', tenantId],
    () => api.listServers(tenantId),
    { refreshInterval: POLL },
  );
  // 아바타 스택·현재 활성 판정을 위해 계정·할당 목록을 같은 SWR 키로 재사용한다.
  const { data: accountsData } = useSWR<AccountPage>(['accounts', tenantId], () => api.listAccounts(tenantId));
  const { data: assignData } = useSWR<AssignmentPage>(['assignments', tenantId], () => api.listAssignments(tenantId));
  useMarkOnData(data);
  const [creating, setCreating] = useState(false);
  const [usageOf, setUsageOf] = useState<Server | null>(null);
  const [policyOf, setPolicyOf] = useState<Server | null>(null);
  const [eventsOf, setEventsOf] = useState<Server | null>(null);
  const [tokenOf, setTokenOf] = useState<EnrollTokenResponse | null>(null);
  const [updateOf, setUpdateOf] = useState<Server | null>(null);
  const act = useAction();
  const now = useNow(1000);
  const servers = data?.items ?? [];

  const accItems = accountsData?.items ?? [];
  const asgItems = assignData?.items ?? [];
  const emailOf = new Map(accItems.map((a) => [a.id, a.email]));
  const activeByServer = currentActiveByServer(asgItems, accItems);
  const accountsByServer = new Map<string, string[]>();
  for (const a of asgItems) {
    const email = emailOf.get(a.accountId);
    if (!email) continue;
    const arr = accountsByServer.get(a.serverId) ?? [];
    arr.push(email);
    accountsByServer.set(a.serverId, arr);
  }

  function toggleMode(s: Server) {
    act.run(
      () => api.setSwitchMode(tenantId, s.id, s.switchMode === 'auto' ? 'manual' : 'auto'),
      () => mutate(),
    );
  }

  const tiles = (
    <div className="srv-tiles">
      {servers.map((s) => {
        const offline = s.status === 'offline';
        const ls = lastSeen(s.lastSeenAt, offline, now);
        const emails = accountsByServer.get(s.id) ?? [];
        const activeId = activeByServer.get(s.id);
        const activeEmail = activeId ? emailOf.get(activeId) : undefined;
        return (
          <div key={s.id} className={`srv-tile ${offline ? 'offline' : ''}`}>
            <div className="srv-tile-head">
              <div style={{ minWidth: 0 }}>
                <div className="srv-tile-name">{s.name}</div>
                {s.hostname && <div className="srv-tile-host">{s.hostname}</div>}
              </div>
              <div className="srv-tile-status">
                <span className={`srv-dot ${s.status}`} aria-hidden="true" />
                <Badge value={s.status} />
              </div>
            </div>
            <div className="srv-tile-meta">
              <span className={`srv-last ${ls.cls}`}>
                {ls.warn ? <Icon name="alert" size={13} /> : <Icon name="clock" size={13} />}
                <span className="mono">{ls.text}</span>
              </span>
              <SwitchModePill mode={s.switchMode} />
            </div>
            <div className="muted mono" style={{ fontSize: 12 }}>
              CPU {fmtPct(s.cpuPct)} · MEM {fmtPct(s.memPct)} · DISK {fmtPct(s.diskPct)}
            </div>
            <div className="srv-tile-accounts">
              {emails.length > 0 ? <AvatarStack emails={emails} /> : <span className="muted" style={{ fontSize: 12 }}>할당 계정 없음</span>}
              {activeEmail && <span className="srv-tile-current">활성 <b>{activeEmail}</b></span>}
            </div>
            <div className="srv-tile-actions">
              <button className="tile-btn" title={s.switchMode === 'auto' ? '수동 전환으로' : '자동 전환으로'} disabled={act.busy} onClick={() => toggleMode(s)}>
                <Icon name={s.switchMode === 'auto' ? 'hand' : 'zap'} size={15} />
              </button>
              <button className="tile-btn" title="사용량 갱신" disabled={act.busy} onClick={() => act.run(() => api.refreshUsage(tenantId, s.id))}>
                <Icon name="refresh" size={15} />
              </button>
              <button className="tile-btn" title="사용량" onClick={() => setUsageOf(s)}><Icon name="gauge" size={15} /></button>
              <button className="tile-btn" title="이벤트" onClick={() => setEventsOf(s)}><Icon name="activity" size={15} /></button>
              <button className="tile-btn" title="정책" onClick={() => setPolicyOf(s)}><Icon name="sliders" size={15} /></button>
              <button
                className="tile-btn"
                title="등록 토큰"
                disabled={act.busy}
                onClick={() => act.run(async () => { const t = await api.issueEnrollToken(tenantId, s.id); setTokenOf(t); })}
              >
                <Icon name="key" size={15} />
              </button>
              <button className="tile-btn" title="에이전트 업데이트" disabled={act.busy} onClick={() => setUpdateOf(s)}>
                <Icon name="rotate" size={15} />
              </button>
              <button className="tile-btn danger" title="삭제" disabled={act.busy} onClick={() => act.run(() => api.deleteServer(tenantId, s.id), () => mutate())}>
                <Icon name="trash" size={15} />
              </button>
            </div>
          </div>
        );
      })}
      {servers.length === 0 && <div className="tile-empty">등록된 서버가 없습니다. &apos;서버 등록&apos;으로 시작하세요.</div>}
    </div>
  );

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>서버<LiveDot /></h2>
        <button className="primary" onClick={() => setCreating(true)}>서버 등록</button>
      </div>
      {act.error && <p className="err">{act.error}</p>}

      {tiles}

      {variant === 'full' && (
      <div className="table-wrap" style={{ marginTop: 16 }}>
        <table>
          <thead>
            <tr>
              <th>이름</th><th>상태</th><th>전환 모드</th><th className="num">할당 계정</th>
              <th className="num">CPU</th><th className="num">MEM</th><th className="num">DISK</th>
              <th>마지막 접속</th><th>동작</th>
            </tr>
          </thead>
          <tbody>
            {servers.map((s) => (
              <tr key={s.id}>
                <td>{s.name}<div className="muted">{s.hostname}</div></td>
                <td><Badge value={s.status} /></td>
                <td><SwitchModePill mode={s.switchMode} /></td>
                <td className="num">{s.assignedAccountCount ?? 0}</td>
                <td className="num mono">{fmtPct(s.cpuPct)}</td>
                <td className="num mono">{fmtPct(s.memPct)}</td>
                <td className="num mono">{fmtPct(s.diskPct)}</td>
                <td><TimeCell iso={s.lastSeenAt} /></td>
                <td>
                  <div className="actions row-actions">
                    <button
                      className="tile-btn"
                      title={s.switchMode === 'auto' ? '수동 전환으로' : '자동 전환으로'}
                      disabled={act.busy}
                      onClick={() =>
                        act.run(
                          () => api.setSwitchMode(tenantId, s.id, s.switchMode === 'auto' ? 'manual' : 'auto'),
                          () => mutate(),
                        )
                      }
                    >
                      <Icon name={s.switchMode === 'auto' ? 'hand' : 'zap'} size={15} />
                    </button>
                    <button
                      className="tile-btn"
                      title="사용량 갱신"
                      disabled={act.busy}
                      onClick={() => act.run(() => api.refreshUsage(tenantId, s.id))}
                    >
                      <Icon name="refresh" size={15} />
                    </button>
                    <button className="tile-btn" title="사용량" onClick={() => setUsageOf(s)}>
                      <Icon name="gauge" size={15} />
                    </button>
                    <button className="tile-btn" title="이벤트" onClick={() => setEventsOf(s)}>
                      <Icon name="activity" size={15} />
                    </button>
                    <button className="tile-btn" title="정책" onClick={() => setPolicyOf(s)}>
                      <Icon name="sliders" size={15} />
                    </button>
                    <button
                      className="tile-btn"
                      title="등록 토큰"
                      disabled={act.busy}
                      onClick={() =>
                        act.run(async () => {
                          const t = await api.issueEnrollToken(tenantId, s.id);
                          setTokenOf(t);
                        })
                      }
                    >
                      <Icon name="key" size={15} />
                    </button>
                    <button className="tile-btn" title="에이전트 업데이트" disabled={act.busy} onClick={() => setUpdateOf(s)}>
                      <Icon name="rotate" size={15} />
                    </button>
                    <button
                      className="tile-btn danger"
                      title="삭제"
                      disabled={act.busy}
                      onClick={() => act.run(() => api.deleteServer(tenantId, s.id), () => mutate())}
                    >
                      <Icon name="trash" size={15} />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
            {servers.length === 0 && (
              <tr><td colSpan={9} className="muted">등록된 서버가 없습니다. '서버 등록'으로 시작하세요.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      )}

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
      {updateOf && (
        <SelfUpdateModal
          tenantId={tenantId}
          server={servers.find((x) => x.id === updateOf.id) ?? updateOf}
          onClose={() => setUpdateOf(null)}
        />
      )}
      {tokenOf && <EnrollTokenModal token={tokenOf} onClose={() => setTokenOf(null)} />}
    </div>
  );
}

// 등록 토큰 모달 — 토큰만 보여주는 대신, 노트북에 그대로 붙여넣을 설치 명령까지 조립해
// 준다. amsEndpoint/amsPubkey는 발급 응답에서 오며(app.config의 AMX_ADVERTISE_HOST·
// 서명키에서 파생), 광고 host 미설정이면 endpoint가 null이라 자리표시자로 대체한다.
// 명령 형식은 deploy/agent-install-cmd.sh와 동일. --insecure는 TLS 전환 전 시험 경로용.
const AGENT_DIR = '~/AMX-agent';
const AGENT_REPO = 'https://github.com/2kwanghee/AMX.git';

function EnrollTokenModal({ token, onClose }: { token: EnrollTokenResponse; onClose: () => void }) {
  const endpoint = token.amsEndpoint || '<서버IP:포트>';
  const pubkey = token.amsPubkey || '<AMS공개키>';
  const install =
    `bash deploy/agent-setup.sh install --ams ${endpoint} ` +
    `--token ${token.token} --pubkey ${pubkey} --insecure`;
  const wslCmd = `cd ${AGENT_DIR} && git pull && ${install}`;
  const winClone = `git clone ${AGENT_REPO} ${AGENT_DIR}`;
  const noEndpoint = !token.amsEndpoint;

  return (
    <Modal title="등록 토큰 (한 번만 표시)" onClose={onClose}>
      <p className="muted">토큰은 지금 한 번만 표시됩니다. 아래 명령을 복사해 대상 노트북에서 실행하세요.</p>

      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', margin: '4px 0 2px' }}>
        <b style={{ fontSize: 13 }}>WSL · Linux</b>
        <CopyButton text={wslCmd} label="명령 복사" />
      </div>
      <pre className="guide-cmd">{wslCmd}</pre>

      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', margin: '12px 0 2px' }}>
        <b style={{ fontSize: 13 }}>Windows · Git Bash</b>
        <CopyButton text={wslCmd} label="명령 복사" />
      </div>
      <p className="muted" style={{ margin: '2px 0' }}>저장소가 없으면 먼저 clone:</p>
      <pre className="guide-cmd">{winClone}</pre>
      <pre className="guide-cmd" style={{ marginTop: 6 }}>{wslCmd}</pre>

      {noEndpoint && (
        <p className="muted" style={{ marginTop: 10 }}>
          광고 주소 미설정 — <code>&lt;서버IP:포트&gt;</code>를 실제 AMS 주소로 바꾸세요.
          서버에 <code>AMX_ADVERTISE_HOST</code>를 지정하면 이 값이 자동으로 채워집니다.
        </p>
      )}

      <p className="muted" style={{ marginTop: 10 }}>
        <b>portproxy · 방화벽</b> — 노트북이 다른 호스트면 PC의 gRPC 포트로 접근 가능해야 합니다.
        WSL에서 구동 시 관리자 PowerShell에서 <code>netsh interface portproxy</code>로 포트를 전달하고,
        방화벽 인바운드를 허용하세요.
      </p>
      <p className="muted">
        <code>--insecure</code>는 평문 경로로, TLS 전환 전 시험 전용입니다. PC도 <code>--insecure-grpc</code>로 떠 있어야 합니다.
      </p>

      <p className="muted" style={{ marginTop: 10 }}>만료 {fmtTime(token.expiresAt)}</p>
    </Modal>
  );
}

// SelfUpdate 2단계 — 에이전트를 최신 코드로 자체 업데이트하도록 명령한다. 에이전트는
// % 진행률을 보고하지 않으므로 단계는 명령 상태(queued→sent→acked/failed)와 servers
// 폴링에서 파생한다: 실행 직후 self-update-status를 2초 주기로 폴링(모달 로컬 SWR 키)
// 하고, acked 이후 servers 폴링에서 lastSeenAt/agentVersion이 바뀌면 완료로 본다. 진행
// 바는 단계 기반(20/40/60/80/100%)이며 연속 %는 만들지 않는다. 실패 시 detail의
// error_code를 노출하고 self_update_failed 경보가 AlertsPanel에 뜬다. 모달을 닫아도
// 무해하다 — 상태는 서버에서 재파생되고, 다시 열면 실행 버튼부터 시작한다.
const SU_STAGES = ['명령 전송됨', '에이전트 수신', '적용 중 · pull·빌드·교체', '적용 완료 · 재시작 중', '완료'];
const SU_DELAY_MS = 10 * 60 * 1000;

function fmtElapsed(ms: number) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return s < 60 ? `${s}초` : `${Math.floor(s / 60)}분 ${s % 60}초`;
}

function SelfUpdateModal({
  tenantId,
  server,
  onClose,
}: {
  tenantId: string;
  server: Server;
  onClose: () => void;
}) {
  const act = useAction();
  const [sent, setSent] = useState(false);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const now = useNow(1000);

  const { data: st } = useSWR<SelfUpdateStatus>(
    sent ? ['self-update-status', tenantId, server.id] : null,
    () => api.getSelfUpdateStatus(tenantId, server.id),
    { refreshInterval: 2000 },
  );

  const status = st?.status ?? null;
  const failed = status === 'failed';
  // Stage 5 (완료): ack 이후의 하트비트(lastSeenAt > ackedAt)만이 exec 재시작 후
  // 재접속을 증명한다. lastSeenAt은 평상시에도 매 하트비트마다 갱신되므로
  // "전송 시점과 다르다" 비교로는 재시작을 판정할 수 없다.
  const completed =
    status === 'acked' &&
    !!st?.ackedAt &&
    !!server.lastSeenAt &&
    Date.parse(server.lastSeenAt) > Date.parse(st.ackedAt);

  // Current 0-based stage. sent covers both "수신" and "적용 중"; the applying
  // step is highlighted since the agent reports no distinct received signal.
  let stage = 0;
  if (status === 'sent') stage = 2;
  if (status === 'acked') stage = 3;
  if (completed) stage = 4;
  const pct = sent ? [20, 40, 60, 80, 100][stage] : 0;

  const sentAtMs = st?.sentAt ? Date.parse(st.sentAt) : null;
  const applyElapsed = status === 'sent' && sentAtMs ? now - sentAtMs : null;
  const delayed =
    sent && !completed && !failed && startedAt !== null && now - startedAt > SU_DELAY_MS;

  function start() {
    setStartedAt(Date.now());
    act.run(() => api.serverSelfUpdate(tenantId, server.id), () => setSent(true));
  }

  return (
    <Modal title={`에이전트 업데이트 — ${server.name}`} onClose={onClose}>
      <p>
        서버 <b>{server.name}</b>의 에이전트를 최신 코드로 업데이트합니다. 업데이트 중 잠시 오프라인이 될 수 있습니다.
      </p>
      <p className="muted">
        현재 버전 <span className="mono">{server.agentVersion || '알 수 없음'}</span>
      </p>
      {act.error && <p className="err">{act.error}</p>}
      {!sent ? (
        <button className="primary" style={{ marginTop: 14 }} disabled={act.busy} onClick={start}>
          업데이트 실행
        </button>
      ) : (
        <div className="su-progress">
          <div className="su-bar">
            <div className={`su-bar-fill ${failed ? 'failed' : ''}`} style={{ width: `${failed ? 100 : pct}%` }} />
          </div>
          <ol className="su-steps">
            {SU_STAGES.map((label, i) => {
              const cls = completed || i < stage ? 'done' : i === stage && !failed ? 'active' : '';
              return (
                <li key={i} className={`su-step ${cls}`}>
                  <span className="su-dot" aria-hidden="true" />
                  <span>
                    {label}
                    {i === 2 && applyElapsed !== null && (
                      <span className="muted mono"> · {fmtElapsed(applyElapsed)} 경과</span>
                    )}
                  </span>
                </li>
              );
            })}
          </ol>
          {failed && (
            <p className="err" style={{ marginTop: 10 }}>
              업데이트 실패{st?.detail ? ` — ${st.detail}` : ''}. 알림 패널을 확인하세요.
            </p>
          )}
          {completed && (
            <p style={{ marginTop: 10 }}>
              업데이트 완료 — 현재 버전 <span className="mono">{server.agentVersion || '알 수 없음'}</span>
            </p>
          )}
          {delayed && (
            <p className="su-warn" style={{ marginTop: 10 }}>
              지연 — 10분이 지나도 완료되지 않았습니다. 알림 패널을 확인하세요.
            </p>
          )}
        </div>
      )}
    </Modal>
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

// Derive a human window label from its span (P2b). claude's canonical windows
// get fixed labels; anything else falls back to a minutes/hours/days rule, or to
// the provider-local id when the span is unknown.
function windowLabel(windowMinutes: number | undefined, id: string): string {
  if (windowMinutes === undefined) return id;
  if (windowMinutes === 300) return '5시간';
  if (windowMinutes === 10080) return '7일';
  if (windowMinutes >= 1440 && windowMinutes % 1440 === 0) return `${windowMinutes / 1440}일`;
  if (windowMinutes >= 60 && windowMinutes % 60 === 0) return `${windowMinutes / 60}시간`;
  return `${windowMinutes}분`;
}

// 창 id는 프로바이더가 정한다 — claude는 five_hour/seven_day, codex는
// primary/secondary(ama-agent/internal/provider/codex/bridge.go). 라벨은 창
// 길이에서 나오므로 codex의 10080분 창이 claude의 "7일"과 글자 그대로 같아진다.
// 한 화면에 두 프로바이더가 섞일 때 어느 쪽 창인지 구분되도록 id로 출처를
// 되짚어 태그를 붙인다. 모르는 id는 태그 없이 둔다(잘못 단정하지 않는다).
const WINDOW_PROVIDER: Record<string, string> = {
  five_hour: 'claude',
  seven_day: 'claude',
  primary: 'codex',
  secondary: 'codex',
};

function windowProvider(id: string): string | undefined {
  return WINDOW_PROVIDER[id];
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
                {(p.accounts ?? []).map((a) => {
                  const windows = a.usage?.windows;
                  return (
                    <Fragment key={a.amsAccountId}>
                      <tr>
                        <td>{a.email}</td>
                        <td><Badge value={a.allocationStatus} /></td>
                        <td>{a.isCurrent ? '★' : ''}</td>
                      </tr>
                      {windows && windows.length > 0 && (
                        <tr className="usage-windows-row">
                          <td colSpan={3}>
                            <div className="usage-windows">
                              {windows.map((w) => {
                                const prov = windowProvider(w.id);
                                return (
                                  <div className="usage-window" key={w.id}>
                                    {prov && (
                                      <span className={`uw-prov ${prov}`} title={`${krLabel(prov)} 창 ${w.id}`}>
                                        <Icon name={providerIcon(prov)} size={10} />
                                        {krLabel(prov)}
                                      </span>
                                    )}
                                    <span className="uw-label">{windowLabel(w.windowMinutes, w.id)}</span>
                                    <span className="uw-pct">{w.pct}%</span>
                                  </div>
                                );
                              })}
                            </div>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
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
