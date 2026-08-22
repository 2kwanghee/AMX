'use client';

import { useEffect, useRef, useState } from 'react';
import { interpolate, interpolateNumber } from 'd3-interpolate';
import { easeOutCubic } from './math';
import { prefersReducedMotion } from './motion';

// path의 명령 문자만 뽑아낸 순서열. 두 path의 명령 구조(M/L/C 개수·순서)가 같으면
// 숫자 개수도 같을 확률이 높고, 그때는 d3-interpolate의 문자열 보간을 그대로 써도
// 각 좌표가 자연스럽게 이어진다.
function commandSignature(d: string): string {
  return (d.match(/[MLCQAZmlcqaz]/g) ?? []).join('');
}

function countNumbers(d: string): number {
  return (d.match(/-?\d*\.?\d+(?:e[-+]?\d+)?/gi) ?? []).length;
}

// SVG path를 오프스크린 <path>에 그려 길이 기준으로 n개 점을 뽑는다. 브라우저
// getPointAtLength에 의존하므로 클라이언트 전용이다. 구조가 다른 두 path(버킷
// 개수가 바뀐 경우 등)를 같은 점 개수로 맞추는 유일한 방법이라 d3-interpolate가
// 다루지 못하는 리샘플 단계를 여기서 대신한다.
function samplePath(d: string, n: number): [number, number][] {
  if (typeof document === 'undefined' || !d) return [];
  const el = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  el.setAttribute('d', d);
  const len = el.getTotalLength();
  if (!Number.isFinite(len) || len <= 0) return [];
  const pts: [number, number][] = [];
  for (let i = 0; i < n; i++) {
    const p = el.getPointAtLength((len * i) / Math.max(1, n - 1));
    pts.push([p.x, p.y]);
  }
  return pts;
}

function buildPolyline(points: [number, number][]): string {
  if (points.length === 0) return '';
  return points.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(2)} ${p[1].toFixed(2)}`).join(' ');
}

const SAMPLE_POINTS = 64;

/**
 * SVG path의 d 속성을 이전 값에서 새 값으로 보간한다. 명령 구조(M/L/C 순서·개수)가
 * 같으면 d3-interpolate의 문자열 보간을 바로 쓰고, 버킷 수가 달라 구조가 어긋나면
 * 두 path를 SAMPLE_POINTS개 점으로 리샘플한 뒤 점 단위로 보간해 폴리라인을
 * 다시 그린다(굴곡은 단순해지지만 항상 보간이 가능하다). reduced-motion이면 즉시
 * 최종 path.
 *
 * mountFrom을 주면 최초 마운트 시에만 그 path에서 d로 보간해 "그려지는" 진입
 * 애니메이션을 만든다(예: 바닥선에서 실제 영역으로, 소스 노드 위치에서 실제
 * 링크로). SSR·최초 렌더값은 항상 d 그대로라 hydration에는 영향이 없다 —
 * mountFrom 보간은 useEffect 안에서만, 클라이언트 마운트 이후에 시작된다.
 * mountFrom을 안 주면 기존과 동일하게 마운트 시엔 애니메이션 없이 최종값이다.
 */
export function useInterpolatedPath(d: string, ms = 600, mountFrom?: string): string {
  const [display, setDisplay] = useState(d);
  const prevRef = useRef(d);
  const mountedRef = useRef(false);

  useEffect(() => {
    const isFirstRun = !mountedRef.current;
    mountedRef.current = true;

    const to = d;
    // 최초 실행에서는 prevRef(=d, 즉 from===to라 애니메이션이 안 도는 문제의
    // 원인)가 아니라 mountFrom을 시작점으로 쓴다. mountFrom이 없으면 기존 동작
    // (마운트 시 애니메이션 없음) 그대로.
    const from = isFirstRun && mountFrom ? mountFrom : prevRef.current;
    prevRef.current = d;

    if (from === to || !from || !to || prefersReducedMotion()) {
      setDisplay(to);
      return;
    }

    const sameStructure = commandSignature(from) === commandSignature(to) && countNumbers(from) === countNumbers(to);

    let interpolator: (t: number) => string;
    if (sameStructure) {
      const strInterp = interpolate(from, to) as (t: number) => string;
      interpolator = strInterp;
    } else {
      const fromPts = samplePath(from, SAMPLE_POINTS);
      const toPts = samplePath(to, SAMPLE_POINTS);
      if (fromPts.length === 0 || toPts.length === 0) {
        setDisplay(to);
        return;
      }
      const pairInterps = fromPts.map((p, i) => {
        const q = toPts[i]!;
        return [interpolateNumber(p[0], q[0]), interpolateNumber(p[1], q[1])] as const;
      });
      interpolator = (t: number) => buildPolyline(pairInterps.map(([ix, iy]) => [ix(t), iy(t)]));
    }

    let raf = 0;
    const start = performance.now();
    const tick = (now: number) => {
      const rawT = Math.min(1, (now - start) / ms);
      setDisplay(interpolator(easeOutCubic(rawT)));
      if (rawT < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [d, ms, mountFrom]);

  return display;
}
