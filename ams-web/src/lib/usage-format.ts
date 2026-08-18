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
