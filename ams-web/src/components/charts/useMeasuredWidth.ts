'use client';

import { useCallback, useRef, useState } from 'react';

/** 컨테이너의 실제 픽셀 폭을 재서 돌려준다.
 *
 *  SVG를 고정 viewBox로 그려 놓고 CSS로 `width: 100%`를 주면 카드가 넓어질수록
 *  내용 전체가 확대된다 — 10px로 지정한 축 글자가 카드 폭 1000px에서는 16px
 *  넘게 보이는 식이다. 그래서 뷰포트 좌표계를 실제 폭에 맞춰 1:1로 잡고,
 *  글자 크기는 지정한 값 그대로 나오게 한다.
 *
 *  useEffect가 아니라 콜백 ref로 붙는다. 차트는 데이터가 오기 전에 "데이터
 *  없음" 블록을 먼저 그리므로, 마운트 시점에 한 번만 도는 effect는 아직 없는
 *  노드를 보고 그냥 돌아가 버린다(그 뒤로 폭이 영영 fallback에 머문다).
 *  콜백 ref는 노드가 실제로 붙는 그 순간에 실행된다.
 *
 *  ResizeObserver가 없는 환경에서는 붙는 시점의 폭으로 한 번만 그린다. */
export function useMeasuredWidth<T extends HTMLElement>(fallback = 640) {
  const [width, setWidth] = useState(fallback);
  const observerRef = useRef<ResizeObserver | null>(null);

  const ref = useCallback((node: T | null) => {
    observerRef.current?.disconnect();
    observerRef.current = null;
    if (!node) return;

    const read = () => {
      const w = Math.round(node.clientWidth);
      if (w > 0) setWidth((prev) => (prev === w ? prev : w));
    };
    read();
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(read);
    ro.observe(node);
    observerRef.current = ro;
  }, []);

  return [ref, width] as const;
}
