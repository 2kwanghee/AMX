'use client';

import { useEffect, useRef, useState } from 'react';
import { countUpValue } from './math';
import { prefersReducedMotion } from './motion';

/**
 * 값이 바뀔 때마다 이전 값에서 새 값으로 rAF로 부드럽게 올린다(또는 내린다).
 * 최초 렌더값은 항상 value 그대로라 서버 렌더와 어긋나지 않고(hydration 안전),
 * 이후 갱신만 애니메이션한다. prefers-reduced-motion이면 매번 즉시 최종값.
 */
export function useCountUp(value: number, ms = 600): number {
  const [display, setDisplay] = useState(value);
  const prevRef = useRef(value);

  useEffect(() => {
    const from = prevRef.current;
    const to = value;
    prevRef.current = value;

    if (from === to || !Number.isFinite(from) || !Number.isFinite(to) || prefersReducedMotion()) {
      setDisplay(to);
      return;
    }

    let raf = 0;
    const start = performance.now();
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / ms);
      setDisplay(countUpValue(from, to, t));
      if (t < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value, ms]);

  return display;
}
