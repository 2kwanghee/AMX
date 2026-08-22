// prefers-reduced-motion 판정. useCountUp·useInterpolatedPath·RankBars가 각자
// 정의하던 걸 하나로 모았다 — 셋 다 판정 기준이 완전히 같아야 하고(하나만 다르게
// 고치면 위젯마다 reduced-motion 반응이 어긋난다), 로직도 한 줄이라 훅으로 감쌀
// 이유는 없다.
export function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true;
}
