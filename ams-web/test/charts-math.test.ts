// src/components/charts/math.ts 순수 함수 단위 테스트. 컴포넌트는 DOM이 없어
// 렌더 테스트가 안 되므로(vitest environment: 'node'), 계산 로직만 여기서 검증한다.
import { describe, expect, it } from 'vitest';
import {
  countUpFrames,
  countUpValue,
  donutArcs,
  easeOutCubic,
  flipPositions,
  formatCompact,
  heatmapScale,
  niceTicks,
  sankeyLayout,
  stackSeries,
  areaPath,
} from '@/components/charts/math';

describe('easeOutCubic / countUpValue / countUpFrames', () => {
  it('0과 1에서 경계값을 정확히 돌려준다', () => {
    expect(easeOutCubic(0)).toBe(0);
    expect(easeOutCubic(1)).toBe(1);
  });

  it('범위를 벗어난 t는 클램프한다', () => {
    expect(easeOutCubic(-1)).toBe(0);
    expect(easeOutCubic(2)).toBe(1);
  });

  it('countUpValue는 t=0에서 from, t=1에서 to를 돌려준다', () => {
    expect(countUpValue(10, 20, 0)).toBe(10);
    expect(countUpValue(10, 20, 1)).toBe(20);
  });

  it('to가 유한하지 않으면 그대로 반환한다', () => {
    expect(countUpValue(0, NaN, 0.5)).toBeNaN();
    expect(countUpValue(NaN, 5, 0.5)).toBe(5);
  });

  it('countUpFrames(frames<=0)는 [to] 하나만 돌려준다', () => {
    expect(countUpFrames(0, 42, 0)).toEqual([42]);
    expect(countUpFrames(0, 42, -3)).toEqual([42]);
  });

  it('countUpFrames는 frames+1개 표본을 만들고 첫값=from, 끝값=to', () => {
    const frames = countUpFrames(0, 100, 4);
    expect(frames).toHaveLength(5);
    expect(frames[0]).toBe(0);
    expect(frames[4]).toBe(100);
  });

  it('to가 유한하지 않으면 빈 배열', () => {
    expect(countUpFrames(0, NaN, 4)).toEqual([]);
  });
});

describe('niceTicks', () => {
  it('min===max면 값 하나짜리 눈금', () => {
    expect(niceTicks(5, 5)).toEqual([5]);
  });

  it('둘 다 0이어도 안전하게 [0]', () => {
    expect(niceTicks(0, 0)).toEqual([0]);
  });

  it('유한하지 않거나 n<=0이면 빈 배열', () => {
    expect(niceTicks(0, NaN)).toEqual([]);
    expect(niceTicks(0, 10, 0)).toEqual([]);
  });

  it('min>max로 뒤집혀 들어와도 정렬해서 처리한다', () => {
    const ticks = niceTicks(100, 0, 5);
    expect(ticks[0]).toBeLessThanOrEqual(0);
    expect(ticks[ticks.length - 1]).toBeGreaterThanOrEqual(100);
  });

  it('일반 구간에서 오름차순 눈금을 만든다', () => {
    const ticks = niceTicks(0, 97, 5);
    expect(ticks.length).toBeGreaterThan(1);
    for (let i = 1; i < ticks.length; i++) expect(ticks[i]!).toBeGreaterThan(ticks[i - 1]!);
    expect(ticks[0]).toBeLessThanOrEqual(0);
    expect(ticks[ticks.length - 1]).toBeGreaterThanOrEqual(97);
  });
});

describe('formatCompact', () => {
  it('1000 미만은 그대로', () => {
    expect(formatCompact(999)).toBe('999');
  });
  it('K/M 단위로 압축한다', () => {
    expect(formatCompact(1200)).toBe('1.2K');
    expect(formatCompact(3_400_000)).toBe('3.4M');
  });
});

describe('stackSeries', () => {
  it('빈 배열이면 빈 배열', () => {
    expect(stackSeries([])).toEqual([]);
  });

  it('단일 시리즈는 y0=0으로 쌓인다', () => {
    const bands = stackSeries([{ key: 'a', values: [10, 20] }]);
    expect(bands).toEqual([{ key: 'a', values: [[0, 10], [0, 20]] }]);
  });

  it('여러 시리즈는 순서대로 누적된다', () => {
    const bands = stackSeries([
      { key: 'a', values: [10] },
      { key: 'b', values: [5] },
    ]);
    expect(bands[0]!.values).toEqual([[0, 10]]);
    expect(bands[1]!.values).toEqual([[10, 15]]);
  });

  it('음수·결측 버킷은 0으로 방어한다', () => {
    const bands = stackSeries([{ key: 'a', values: [-5, NaN] }]);
    expect(bands[0]!.values).toEqual([[0, 0], [0, 0]]);
  });

  it('시리즈마다 버킷 길이가 달라도 짧은 쪽은 0으로 채운다', () => {
    const bands = stackSeries([
      { key: 'a', values: [1, 2, 3] },
      { key: 'b', values: [1] },
    ]);
    expect(bands[1]!.values).toEqual([[1, 2], [2, 2], [3, 3]]);
  });
});

describe('donutArcs', () => {
  it('빈 입력이면 빈 배열', () => {
    expect(donutArcs([])).toEqual([]);
  });

  it('합계가 0이면 빈 배열', () => {
    expect(donutArcs([{ key: 'a', value: 0 }])).toEqual([]);
  });

  it('단일 값은 fraction=1, path가 생성된다', () => {
    const segs = donutArcs([{ key: 'a', value: 10 }]);
    expect(segs).toHaveLength(1);
    expect(segs[0]!.fraction).toBe(1);
    expect(segs[0]!.path.length).toBeGreaterThan(0);
  });

  it('두 값의 fraction 합은 1', () => {
    const segs = donutArcs([
      { key: 'a', value: 30 },
      { key: 'b', value: 70 },
    ]);
    const sum = segs.reduce((s, x) => s + x.fraction, 0);
    expect(sum).toBeCloseTo(1);
  });
});

describe('areaPath', () => {
  it('빈 배열이면 빈 문자열', () => {
    expect(areaPath([], 100, 50, 10)).toBe('');
  });

  it('점이 하나여도 path 문자열을 만든다', () => {
    const d = areaPath([[0, 5]], 100, 50, 10);
    expect(d.length).toBeGreaterThan(0);
  });

  it('maxY<=0이면 예외 없이 바닥선으로 눌러 그린다', () => {
    expect(() => areaPath([[0, 5], [0, 8]], 100, 50, 0)).not.toThrow();
  });
});

describe('sankeyLayout', () => {
  it('노드가 없으면 빈 결과', () => {
    expect(sankeyLayout([], [], 100, 100)).toEqual({ nodes: [], links: [] });
  });

  it('유효한 링크가 없으면(값 0, 자기참조, 존재하지 않는 노드) 빈 결과', () => {
    const nodes = [{ id: 's1', label: 'S1', kind: 'server' }];
    expect(sankeyLayout(nodes, [{ source: 's1', target: 's1', value: 5 }], 100, 100)).toEqual({
      nodes: [],
      links: [],
    });
    expect(sankeyLayout(nodes, [{ source: 's1', target: 'missing', value: 5 }], 100, 100)).toEqual({
      nodes: [],
      links: [],
    });
    expect(sankeyLayout(nodes, [{ source: 's1', target: 's1', value: 0 }], 100, 100)).toEqual({
      nodes: [],
      links: [],
    });
  });

  it('유효한 링크 하나는 노드 2개·링크 1개로 배치된다', () => {
    const nodes = [
      { id: 's1', label: 'S1', kind: 'server' },
      { id: 'a1', label: 'A1', kind: 'account' },
    ];
    const result = sankeyLayout(nodes, [{ source: 's1', target: 'a1', value: 10 }], 200, 100);
    expect(result.nodes).toHaveLength(2);
    expect(result.links).toHaveLength(1);
    expect(result.links[0]!.source).toBe('s1');
    expect(result.links[0]!.target).toBe('a1');
    expect(result.links[0]!.path.length).toBeGreaterThan(0);
  });
});

describe('heatmapScale', () => {
  it('max<=0이면 항상 0', () => {
    const scale = heatmapScale(0);
    expect(scale(5)).toBe(0);
    expect(scale(-1)).toBe(0);
  });

  it('값을 0~1로 정규화하고 상한을 클램프한다', () => {
    const scale = heatmapScale(10);
    expect(scale(0)).toBe(0);
    expect(scale(5)).toBe(0.5);
    expect(scale(10)).toBe(1);
    expect(scale(100)).toBe(1);
  });

  it('음수·NaN 값은 0', () => {
    const scale = heatmapScale(10);
    expect(scale(-5)).toBe(0);
    expect(scale(NaN)).toBe(0);
  });
});

describe('flipPositions', () => {
  it('둘 다 비어 있으면 빈 배열', () => {
    expect(flipPositions([], [])).toEqual([]);
  });

  it('순서가 같으면 deltaIndex는 전부 0', () => {
    const deltas = flipPositions(['a', 'b', 'c'], ['a', 'b', 'c']);
    expect(deltas.every((d) => d.deltaIndex === 0)).toBe(true);
  });

  it('새로 등장한 항목은 prevIndex=-1, deltaIndex=0', () => {
    const deltas = flipPositions([], ['a']);
    expect(deltas).toEqual([{ key: 'a', prevIndex: -1, nextIndex: 0, deltaIndex: 0 }]);
  });

  it('순위가 뒤집히면 deltaIndex가 반대 부호로 나온다', () => {
    const deltas = flipPositions(['a', 'b'], ['b', 'a']);
    const a = deltas.find((d) => d.key === 'a')!;
    const b = deltas.find((d) => d.key === 'b')!;
    expect(a.deltaIndex).toBe(-1); // a: 0 -> 1, 아래로 한 칸
    expect(b.deltaIndex).toBe(1); // b: 1 -> 0, 위로 한 칸
  });
});
