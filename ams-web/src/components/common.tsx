'use client';

import type { ReactNode, SVGProps } from 'react';
import { useEffect, useRef, useState } from 'react';
import { krApiError } from '@/lib/api-client/client';

// 상태·유형 값의 한글 표시 매핑. className은 원래 영문 값을 유지하고 표시만
// 한글로 바꾼다. 매핑에 없는 값은 원문 그대로 반환한다.
const KR_LABEL: Record<string, string> = {
  online: '온라인',
  offline: '오프라인',
  degraded: '성능 저하',
  quarantined: '격리됨',
  active: '활성',
  assigned: '연결됨',
  available: '사용 가능',
  pending: '대기',
  delivering: '전달 중',
  recalling: '회수 중',
  detached: '해제됨',
  inactive: '비활성',
  disabled: '비활성',
  critical: '심각',
  warning: '경고',
  open: '미확인',
  acked: '확인됨',
  resolved: '해결됨',
  manual: '수동',
  auto: '자동',
  oauth: 'OAuth',
  api_key: 'API 키',
  claude: 'Claude',
  codex: 'Codex',
};

export function krLabel(value?: string): string {
  if (!value) return '—';
  return KR_LABEL[value] ?? value;
}

// -- 인라인 아이콘 세트 -------------------------------------------------------
// 외부 리소스 없이 stroke 기반 Lucide풍 단순 패스를 직접 작성한다. 모든 아이콘은
// currentColor를 따르며 24x24 viewBox 위에 그린다. name으로 골라 재사용한다.
export type IconName =
  | 'grid'
  | 'server'
  | 'user'
  | 'link'
  | 'bell'
  | 'send'
  | 'power'
  | 'pause'
  | 'rotate'
  | 'zap'
  | 'undo'
  | 'hand'
  | 'clock'
  | 'trash'
  | 'key'
  | 'sliders'
  | 'gauge'
  | 'activity'
  | 'alert'
  | 'refresh'
  | 'check'
  | 'help'
  | 'claude'
  | 'codex';

const ICON_PATHS: Record<IconName, ReactNode> = {
  grid: (
    <>
      <rect x="3" y="3" width="7" height="7" rx="1.5" />
      <rect x="14" y="3" width="7" height="7" rx="1.5" />
      <rect x="3" y="14" width="7" height="7" rx="1.5" />
      <rect x="14" y="14" width="7" height="7" rx="1.5" />
    </>
  ),
  server: (
    <>
      <rect x="3" y="4" width="18" height="7" rx="2" />
      <rect x="3" y="13" width="18" height="7" rx="2" />
      <line x1="7" y1="7.5" x2="7.01" y2="7.5" />
      <line x1="7" y1="16.5" x2="7.01" y2="16.5" />
    </>
  ),
  user: (
    <>
      <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
      <circle cx="12" cy="7" r="4" />
    </>
  ),
  link: (
    <>
      <circle cx="6" cy="12" r="3" />
      <circle cx="18" cy="12" r="3" />
      <line x1="9" y1="12" x2="15" y2="12" />
    </>
  ),
  bell: (
    <>
      <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
      <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
    </>
  ),
  send: (
    <>
      <path d="M22 2 11 13" />
      <path d="M22 2 15 22l-4-9-9-4Z" />
    </>
  ),
  power: (
    <>
      <path d="M12 2v10" />
      <path d="M18.36 6.64a9 9 0 1 1-12.73 0" />
    </>
  ),
  pause: (
    <>
      <rect x="6" y="4" width="4" height="16" rx="1" />
      <rect x="14" y="4" width="4" height="16" rx="1" />
    </>
  ),
  rotate: (
    <>
      <path d="M3 2v6h6" />
      <path d="M21 12A9 9 0 0 0 6 5.3L3 8" />
    </>
  ),
  zap: <path d="M13 2 3 14h9l-1 8 10-12h-9l1-8Z" />,
  undo: (
    <>
      <path d="M9 14 4 9l5-5" />
      <path d="M4 9h11a5 5 0 0 1 0 10h-1" />
    </>
  ),
  hand: (
    <>
      <path d="M18 11V6a2 2 0 0 0-4 0" />
      <path d="M14 10V4a2 2 0 0 0-4 0v2" />
      <path d="M10 10.5V6a2 2 0 0 0-4 0v8" />
      <path d="M18 8a2 2 0 1 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.86-5.99-2.34l-3.6-3.6a2 2 0 0 1 2.83-2.82L7 15" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </>
  ),
  trash: (
    <>
      <path d="M3 6h18" />
      <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
    </>
  ),
  key: (
    <>
      <circle cx="7.5" cy="15.5" r="4.5" />
      <path d="M10.7 12.3 21 2" />
      <path d="M16.5 6.5 20 10" />
    </>
  ),
  sliders: (
    <>
      <line x1="4" y1="6" x2="20" y2="6" />
      <circle cx="9" cy="6" r="2" />
      <line x1="4" y1="12" x2="20" y2="12" />
      <circle cx="15" cy="12" r="2" />
      <line x1="4" y1="18" x2="20" y2="18" />
      <circle cx="8" cy="18" r="2" />
    </>
  ),
  gauge: (
    <>
      <path d="M3.5 18a9 9 0 1 1 17 0" />
      <path d="M12 14 15.5 9.5" />
      <circle cx="12" cy="14" r="1.4" />
    </>
  ),
  activity: <path d="M22 12h-4l-3 9L9 3l-3 9H2" />,
  alert: (
    <>
      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </>
  ),
  refresh: (
    <>
      <path d="M21 12a9 9 0 1 1-2.64-6.36" />
      <path d="M21 3v6h-6" />
    </>
  ),
  check: <path d="M20 6 9 17l-5-5" />,
  help: (
    <>
      <circle cx="12" cy="12" r="10" />
      <path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 2.5-3 4" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </>
  ),
  // Claude — 4각 스파클(큰 별 + 작은 별) 조합
  claude: (
    <>
      <path d="M12 3c.4 3.4 1.6 4.6 5 5-3.4.4-4.6 1.6-5 5-.4-3.4-1.6-4.6-5-5 3.4-.4 4.6-1.6 5-5Z" />
      <path d="M18 14c.2 1.4.6 1.8 2 2-1.4.2-1.8.6-2 2-.2-1.4-.6-1.8-2-2 1.4-.2 1.8-.6 2-2Z" />
    </>
  ),
  // Codex — 코드 꺾쇠 </> (좌우 셰브런 + 사선)
  codex: (
    <>
      <path d="m8 8-4 4 4 4" />
      <path d="m16 8 4 4-4 4" />
      <line x1="13.5" y1="6" x2="10.5" y2="18" />
    </>
  ),
};

export function Icon({
  name,
  size = 16,
  ...rest
}: { name: IconName; size?: number } & Omit<SVGProps<SVGSVGElement>, 'name'>) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...rest}
    >
      {ICON_PATHS[name]}
    </svg>
  );
}

// 관제망 메시 — 계정→서버 배급망을 추상화한 장식 SVG. currentColor(--accent)
// stroke, 매우 낮은 opacity, pointer-events 없음, aria-hidden. 배경 레이어로만
// 쓰며 콘텐츠는 위에 불투명하게 쌓인다. 연결선 1개에 흐르는 점 애니메이션(CSS,
// reduced-motion 시 정지). variant로 배치·강도를 구분한다.
export function MeshBackdrop({ variant = 'dashboard' }: { variant?: 'dashboard' | 'login' }) {
  return (
    <svg
      className={`mesh-backdrop ${variant}`}
      viewBox="0 0 600 480"
      width="600"
      height="480"
      fill="none"
      aria-hidden="true"
      focusable="false"
    >
      <g className="mesh-link">
        <path d="M80 120 Q110 200 180 240" />
        <path d="M200 60 Q280 80 320 140" />
        <path d="M320 140 Q400 150 440 220" />
        <path d="M180 240 Q230 300 300 320" />
        <path d="M300 320 Q330 390 400 420" />
        <path d="M440 220 Q500 260 520 340" />
        <path d="M320 140 Q240 180 180 240" />
        <path d="M300 320 Q380 300 440 220" />
      </g>
      <g className="mesh-node">
        <circle cx="80" cy="120" r="6" />
        <circle cx="200" cy="60" r="5" />
        <circle cx="320" cy="140" r="7" />
        <circle cx="180" cy="240" r="6" />
        <circle cx="300" cy="320" r="7" />
        <circle cx="440" cy="220" r="6" />
        <circle cx="520" cy="340" r="5" />
        <circle cx="400" cy="420" r="6" />
      </g>
      {/* n3 → n6 연결선을 따라 흐르는 점 */}
      <circle className="mesh-flow-dot" r="3.5" />
    </svg>
  );
}

export function Badge({ value }: { value: string }) {
  return (
    <span className={`badge ${value}`}>
      <span className="dot" />
      {krLabel(value)}
    </span>
  );
}

// provider 값 → 고정 아이콘. claude/codex 외에는 user로 폴백한다.
export function providerIcon(provider?: string): IconName {
  if (provider === 'claude') return 'claude';
  if (provider === 'codex') return 'codex';
  return 'user';
}

// provider 전용 태그 — 상태 Badge와 분리된 공용 단위. provider 값에 따라 아이콘·
// 색이 자동으로 정해지며(수동 지정 없음), 한글 라벨은 그대로 유지한다.
export function ProviderTag({ value }: { value: string }) {
  return (
    <span className={`prov-tag ${value}`}>
      <Icon name={providerIcon(value)} size={13} />
      {krLabel(value)}
    </span>
  );
}

// 랙 유닛 실루엣 — NMS 장비 은유. 케이스 + 가로 슬랫 3개 + 우측 상태 LED 3개.
// currentColor stroke를 따르며 노드 헤드에서 서버 아이콘으로 쓴다.
export function RackGlyph({ size = 26 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="4" y="3.5" width="16" height="17" rx="2.2" />
      <line x1="7.2" y1="7.5" x2="14" y2="7.5" />
      <line x1="7.2" y1="12" x2="14" y2="12" />
      <line x1="7.2" y1="16.5" x2="14" y2="16.5" />
      <circle cx="16.8" cy="7.5" r="0.95" fill="currentColor" stroke="none" />
      <circle cx="16.8" cy="12" r="0.95" fill="currentColor" stroke="none" />
      <circle cx="16.8" cy="16.5" r="0.95" fill="currentColor" stroke="none" />
    </svg>
  );
}

// 에이전트 로봇 헤드 아바타 — 안테나 + 눈 2개 + 스마일 아크. 계정 노드의 캡슐
// 아바타로 쓰며 프로바이더 색(claude=틸, codex=인디고)을 currentColor로 상속한다.
export function RobotAvatar({ size = 24 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <line x1="12" y1="2.6" x2="12" y2="5" />
      <circle cx="12" cy="2" r="1.1" fill="currentColor" stroke="none" />
      <rect x="4.5" y="5" width="15" height="12" rx="3.2" />
      <circle cx="9" cy="10.2" r="1.25" fill="currentColor" stroke="none" />
      <circle cx="15" cy="10.2" r="1.25" fill="currentColor" stroke="none" />
      <path d="M8.8 13.2a3.4 3.4 0 0 0 6.4 0" />
    </svg>
  );
}

// 전환 모드 알약 — 수동=손 아이콘(회색), 자동=번개 아이콘(액센트).
export function SwitchModePill({ mode }: { mode: string }) {
  const auto = mode === 'auto';
  return (
    <span className={`mode-pill ${auto ? 'auto' : 'manual'}`}>
      <Icon name={auto ? 'zap' : 'hand'} size={13} />
      {krLabel(mode)}
    </span>
  );
}

// 이메일 아바타 칩 — 이니셜 1글자 원형. 색은 이메일 해시로 4색 중 선택한다.
function hashIndex(s: string, mod: number): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h % mod;
}

export function EmailChip({ email, sub }: { email: string; sub?: string }) {
  const initial = (email.trim()[0] ?? '?').toUpperCase();
  const idx = hashIndex(email, 4);
  return (
    <span className="email-cell">
      <span className={`avatar avatar-${idx}`} aria-hidden="true">{initial}</span>
      <span className="email-text">
        {email}
        {sub && <span className="muted email-sub">{sub}</span>}
      </span>
    </span>
  );
}

// 상대 시각 표기("3분 전"). 24시간 이상이면 날짜로 표기하고, 절대시각은 호출부의
// title 속성으로 노출한다(TimeCell 참조).
export function relTime(iso?: string): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const diff = Date.now() - d.getTime();
  const sec = Math.floor(diff / 1000);
  if (sec < 0) return fmtTime(iso);
  if (sec < 60) return '방금 전';
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}분 전`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}시간 전`;
  return d.toLocaleDateString('ko-KR', { month: 'short', day: 'numeric' });
}

// 시각 셀 — 시계 아이콘 + 상대시간, title에 절대시각.
export function TimeCell({ iso }: { iso?: string }) {
  if (!iso) return <span className="muted">—</span>;
  return (
    <span className="time-cell" title={fmtTime(iso)}>
      <Icon name="clock" size={12} />
      {relTime(iso)}
    </span>
  );
}

export function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h2>{title}</h2>
          <button onClick={onClose} aria-label="닫기">✕</button>
        </div>
        {children}
      </div>
    </div>
  );
}

/** 클립보드 복사 버튼. 누르면 1.5초간 "복사됨"을 표시한다.
 *  http LAN 콘솔은 insecure context라 navigator.clipboard가 없다 —
 *  execCommand('copy')로 폴백하고, 그래도 실패하면 실패 피드백을 남긴다. */
export function CopyButton({ text, label = '복사' }: { text: string; label?: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle');
  return (
    <button
      type="button"
      className="copy-btn"
      onClick={async () => {
        const ok = await copyText(text);
        setState(ok ? 'copied' : 'failed');
        setTimeout(() => setState('idle'), 1500);
      }}
    >
      {state === 'copied' ? '복사됨' : state === 'failed' ? '복사 실패 — 수동 복사' : label}
    </button>
  );
}

/** 클립보드 API가 없거나 실패하면 execCommand('copy')로 폴백한다. */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* 폴백으로 진행 */
  }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.top = '-1000px';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

/** Wraps an async action with busy + error state, for buttons/forms. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function run(fn: () => Promise<unknown>, onDone?: () => void) {
    setBusy(true);
    setError('');
    try {
      await fn();
      onDone?.();
    } catch (e) {
      setError(krApiError(e));
    } finally {
      setBusy(false);
    }
  }
  return { busy, error, run, setError };
}

export function fmtTime(iso?: string): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString('ko-KR', { dateStyle: 'short', timeStyle: 'short' });
}

// HH:MM:SS 모노 시계 표기. 관제 텔레메트리·활동 피드 타임스탬프에 공통 사용.
export function fmtClock(t: number | Date): string {
  const d = typeof t === 'number' ? new Date(t) : t;
  if (Number.isNaN(d.getTime())) return '--:--:--';
  return d.toLocaleTimeString('ko-KR', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

// 1초(기본) 틱 훅. setInterval + cleanup으로 언마운트 시 누수 없음.
export function useNow(intervalMs = 1000): number {
  // 초기값을 Date.now()로 두면 SSR 프리렌더 시각과 클라이언트 hydration 시각이
  // 달라 "N초 전" 류 텍스트가 어긋나 hydration mismatch(React #418)가 난다. 서버·
  // 클라가 동일한 0으로 시작하고, 마운트 직후 실제 시각으로 올려 CSR에서만 갱신한다.
  const [now, setNow] = useState(0);
  useEffect(() => {
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

// -- SWR 응답 도착 시각 공유 (콘솔 헤더 "갱신 HH:MM:SS") --------------------
// 모듈 스코프 pub/sub. 각 패널이 데이터 수신 시 markDataArrived()를 호출하고,
// 헤더는 useLastDataAt()로 최신 도착 시각을 구독한다. 메모리 내에서만 동작하며
// SWR 키·폴링을 건드리지 않는다.
let lastDataAt = 0;
const dataSubs = new Set<() => void>();
export function markDataArrived(): void {
  lastDataAt = Date.now();
  dataSubs.forEach((f) => f());
}
export function useLastDataAt(): number {
  const [, force] = useState(0);
  useEffect(() => {
    const f = () => force((x) => x + 1);
    dataSubs.add(f);
    return () => { dataSubs.delete(f); };
  }, []);
  return lastDataAt;
}

// data가 새로 도착할 때마다 markDataArrived를 호출하는 헬퍼 훅.
export function useMarkOnData(data: unknown): void {
  useEffect(() => {
    if (data !== undefined) markDataArrived();
  }, [data]);
}

// 폴링 중 표시하는 미세 라이브 도트(패널 제목 옆). reduced-motion 시 정지.
export function LiveDot({ label = '실시간' }: { label?: string }) {
  return <span className="live-dot" title={label} aria-label={label} role="img" />;
}

// LIVE 텔레메트리 헤더 스트립 — 제목 + (LIVE 펄스 · 갱신 시각 · 현재 시각).
export function ConsoleHeader({ title }: { title: string }) {
  const now = useNow(1000);
  const updatedAt = useLastDataAt();
  return (
    <div className="console-head">
      <h1>{title}</h1>
      <div className="telemetry" aria-live="off">
        <span className="live-pill"><span className="live-pip" />LIVE</span>
        <span className="tele-updated">갱신 <span className="mono">{updatedAt ? fmtClock(updatedAt) : '--:--:--'}</span></span>
        <span className="tele-clock mono"><Icon name="clock" size={13} />{fmtClock(now)}</span>
      </div>
    </div>
  );
}

// -- 인라인 스파크라인 (단일 시리즈: 축·격자·범례 없음) ---------------------
// 시리즈색은 CSS currentColor로 카드 고유색을 상속받는다(텍스트엔 쓰지 않음).
// 포인트 2개 미만이면 렌더하지 않는다. 값 변동이 없으면 평평한 선이 정상이다.
export function Sparkline({ data, height = 30 }: { data: number[]; height?: number }) {
  if (data.length < 2) return null;
  const w = 120;
  const pad = 3;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const span = max - min || 1;
  const stepX = (w - pad * 2) / (data.length - 1);
  const pts = data.map((v, i) => {
    const x = pad + i * stepX;
    const y = pad + (height - pad * 2) * (1 - (v - min) / span);
    return [x, y] as const;
  });
  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ');
  const firstX = pad;
  const lastX = pad + (data.length - 1) * stepX;
  const area = `${line} L${lastX.toFixed(1)} ${height - pad} L${firstX.toFixed(1)} ${height - pad} Z`;
  return (
    <svg
      className="spark"
      viewBox={`0 0 ${w} ${height}`}
      width={w}
      height={height}
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <path className="spark-area" d={area} />
      <path className="spark-line" d={line} />
    </svg>
  );
}

// 단일 아바타(이니셜 원형, 이메일 해시 4색). 스택·인라인에서 재사용.
export function Avatar({ email, size = 22 }: { email: string; size?: number }) {
  const initial = (email.trim()[0] ?? '?').toUpperCase();
  const idx = hashIndex(email, 4);
  return (
    <span
      className={`avatar avatar-${idx}`}
      style={{ width: size, height: size, fontSize: Math.round(size * 0.46) }}
      title={email}
      aria-hidden="true"
    >
      {initial}
    </span>
  );
}

// 겹치는 아바타 스택(-8px). 최대 max개 표시 후 나머지는 +k.
export function AvatarStack({ emails, max = 4 }: { emails: string[]; max?: number }) {
  const shown = emails.slice(0, max);
  const extra = emails.length - shown.length;
  return (
    <span className="avatar-stack">
      {shown.map((e, i) => (
        <span key={`${e}-${i}`} className="avatar-slot"><Avatar email={e} /></span>
      ))}
      {extra > 0 && <span className="avatar-more" title={`외 ${extra}명`}>+{extra}</span>}
    </span>
  );
}

// 캡 걸린 히스토리 축적기(스파크라인용). onSuccess 등 렌더 밖에서 push 후
// bump으로 리렌더한다. 메모리 내 useRef만 사용.
export function useSeries(cap = 24): { push: (v: number) => void; data: number[] } {
  const ref = useRef<number[]>([]);
  const [, bump] = useState(0);
  function push(v: number) {
    ref.current = [...ref.current, v].slice(-cap);
    bump((x) => x + 1);
  }
  return { push, data: ref.current };
}
