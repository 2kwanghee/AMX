// 차트 전용 색 팔레트. globals.css의 KPI 4색 토큰(--kpi-teal/indigo/amber/rose)만
// 재사용한다. 새 hex는 넣지 않는다 — 시리즈가 5개를 넘어가면 같은 4색을
// color-mix로 밝혀서 우려낸다(신규 색상이 아니라 기존 토큰의 변형이다).

const TONE_VARS = ['--kpi-teal', '--kpi-indigo', '--kpi-amber', '--kpi-rose'] as const;

/** 카테고리 색상 순환. index가 팔레트 길이를 넘어가면 같은 색을 옅게 우려서
 *  구분한다. "other" 항목은 이 함수를 쓰지 않고 --muted를 직접 쓴다. */
export function seriesColor(index: number): string {
  const base = `var(${TONE_VARS[index % TONE_VARS.length]})`;
  const round = Math.floor(index / TONE_VARS.length);
  if (round <= 0) return base;
  const mixPct = Math.max(25, 65 - round * 20);
  return `color-mix(in srgb, ${base} ${mixPct}%, var(--surface))`;
}

/** "기타" 합산 항목처럼 카테고리에 속하지 않는 값에 쓰는 중립색. */
export const OTHER_COLOR = 'var(--muted)';
