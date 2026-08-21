// 사용량 지표의 단위 표기. LangfuseUsagePanel에서 떼어내 node 테스트 환경에서
// 단위 테스트가 되게 한다(DOM·React 불필요) — event-format.ts와 같은 이유다.
//
// 토큰은 자릿수가 모델·기간에 따라 24에서 수십억까지 벌어진다. 자릿수 구분만
// 넣으면 열 너비가 들쭉날쭉해 비교가 어려워서 K/M/B로 압축하고, 정확한 값은
// 호출부가 title 툴팁으로 붙인다. 압축은 표시 전용이고 계산에는 쓰지 않는다.

/** 자릿수 구분만 넣은 정확한 값. 툴팁과 상세 표기에 쓴다. */
export function fmtExact(n: number): string {
  return Number.isFinite(n) ? Math.round(n).toLocaleString('en-US') : '—';
}

// 경계는 반올림 뒤 기준이다. 999,950을 1e3으로 나누면 999.95 → "1000.0K"가 되어
// 다음 단위를 침범하므로, 그 값은 애초에 M으로 올려 "1.0M"으로 떨어뜨린다.
const SCALES: [limit: number, div: number, suffix: string][] = [
  [999.95e6, 1e9, 'B'],
  [999.95e3, 1e6, 'M'],
  [999.95, 1e3, 'K'],
];

/**
 * 토큰 수를 K/M/B로 압축한다. 1000 미만은 그대로 두고, 그 위는 소수 한 자리로
 * 고정해 열 너비를 일정하게 유지한다. 유한하지 않은 값은 "—".
 */
export function fmtTokens(n: number): string {
  if (!Number.isFinite(n)) return '—';
  const v = Math.round(n);
  const abs = Math.abs(v);
  for (const [limit, div, suffix] of SCALES) {
    if (abs >= limit) return `${(v / div).toFixed(1)}${suffix}`;
  }
  return String(v);
}

/**
 * 호출(관측) 수. 토큰과 달리 자릿수가 크지 않아 압축하지 않고 정확한 값에 단위만
 * 붙인다 — 압축하면 162와 1.6K를 같은 열에서 비교하기 어려워진다.
 */
export function fmtCalls(n: number): string {
  return Number.isFinite(n) ? `${fmtExact(n)}회` : '—';
}

// -- 사용량 스냅샷 payload 선택 로직 --------------------------------------------
// GET …/servers/{sid}/usage 의 payload 는 proto UsageReport 를
// MessageToDict(preserving_proto_field_name=True) 로 저장한 snake_case dict 다
// (types.ts UsagePayload 주석 참조). 모달 JSX 에 박히면 테스트가 어려워서 키를
// 읽는 순수 함수를 여기로 뺀다. event-format.ts·fmt* 와 같은 이유다.

import type { UsageAccount, UsagePayload } from '@/lib/api-client/types';

/** 풀 최대 사용률(%). 값이 없으면 undefined 이며 호출부가 표기 폴백을 정한다. */
export function poolMaxUtilization(p: UsagePayload | undefined): number | undefined {
  return p?.pool_summary?.max_utilization_pct;
}

/** 전 계정 소진 여부. MessageToDict 는 false 를 생략하므로 명시적 true 만 참. */
export function poolAllExhausted(p: UsagePayload | undefined): boolean {
  return p?.pool_summary?.all_exhausted === true;
}

/**
 * proto AllocationStatus enum 이름("ALLOCATION_STATUS_ACTIVE")을 Badge·krLabel 이
 * 아는 소문자 상태어("active")로 줄인다. 접두어가 없으면 그대로 소문자화한다.
 */
export function allocationStatusLabel(raw: string | undefined): string {
  if (!raw) return '';
  return raw.replace(/^ALLOCATION_STATUS_/, '').toLowerCase();
}

// 모달 한 행이 그리는 창. id 는 프로바이더 태그용, windowMinutes 는 라벨용.
export interface UsageWindowView {
  key: string;
  id: string;
  pct: number | undefined;
  windowMinutes: number | undefined;
}

/**
 * 계정별 표시 창 목록. windows[] 를 우선 쓰고, 비어 있으면 legacy 위치형
 * five_hour/seven_day 로 폴백한다(P2b 이중 기록 이전 보고 호환). 위치형은 창
 * 길이가 고정이라 windowMinutes 를 300·10080 으로 채워 라벨이 "5시간"·"7일"로
 * 떨어지게 한다.
 */
export function accountWindows(a: UsageAccount): UsageWindowView[] {
  const ws = a.windows;
  if (ws && ws.length > 0) {
    return ws.map((w, i) => ({
      key: w.id ?? String(i),
      id: w.id ?? '',
      pct: w.pct,
      windowMinutes: w.window_minutes,
    }));
  }
  const fallback: UsageWindowView[] = [];
  if (a.five_hour) {
    fallback.push({ key: 'five_hour', id: 'five_hour', pct: a.five_hour.pct, windowMinutes: 300 });
  }
  if (a.seven_day) {
    fallback.push({ key: 'seven_day', id: 'seven_day', pct: a.seven_day.pct, windowMinutes: 10080 });
  }
  return fallback;
}

// 모달 표의 한 계정 행. 실제 payload 키(account.ams_account_id 등)를 읽어 채운다.
export interface UsageAccountView {
  amsAccountId: string | undefined;
  email: string | undefined;
  allocationStatus: string;
  isCurrent: boolean;
  windows: UsageWindowView[];
}

/** 계정 항목을 모달 표가 쓰는 형태로 정규화한다. 식별자는 중첩된
 * account.ams_account_id 에 있다(proto AccountUsage.account = AccountRef). */
export function usageAccountView(a: UsageAccount): UsageAccountView {
  return {
    amsAccountId: a.account?.ams_account_id,
    email: a.account?.email,
    allocationStatus: allocationStatusLabel(a.allocation_status),
    isCurrent: a.is_current === true,
    windows: accountWindows(a),
  };
}
