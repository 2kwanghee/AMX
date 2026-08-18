import { describe, expect, it } from 'vitest';

import { fmtCalls, fmtExact, fmtTokens } from '@/lib/usage-format';

describe('fmtTokens', () => {
  it('1000 미만은 압축하지 않는다', () => {
    expect(fmtTokens(0)).toBe('0');
    expect(fmtTokens(24)).toBe('24');
    expect(fmtTokens(999)).toBe('999');
  });

  it('K·M·B로 압축하고 소수 한 자리로 고정한다', () => {
    expect(fmtTokens(1000)).toBe('1.0K');
    expect(fmtTokens(1203)).toBe('1.2K');
    // 2026-08-17 실측(sonnet): 입력 24 + 캐시읽기 557,720 + 캐시생성 57,262
    expect(fmtTokens(557_720)).toBe('557.7K');
    expect(fmtTokens(615_006)).toBe('615.0K');
    expect(fmtTokens(2_145_330)).toBe('2.1M');
    expect(fmtTokens(3_400_000_000)).toBe('3.4B');
  });

  it('반올림이 다음 단위를 침범하지 않는다', () => {
    // 999.95e3 / 1e3 = 999.95 → "1000.0K"가 되면 안 된다.
    expect(fmtTokens(999_950)).toBe('1.0M');
    expect(fmtTokens(999_949)).toBe('999.9K');
    expect(fmtTokens(999_950_000)).toBe('1.0B');
  });

  it('유한하지 않은 값은 대시로 떨어진다', () => {
    expect(fmtTokens(Number.NaN)).toBe('—');
    expect(fmtTokens(Number.POSITIVE_INFINITY)).toBe('—');
  });
});

describe('fmtCalls', () => {
  it('압축하지 않고 회를 붙인다', () => {
    expect(fmtCalls(0)).toBe('0회');
    expect(fmtCalls(50)).toBe('50회');
    expect(fmtCalls(162)).toBe('162회');
    expect(fmtCalls(12_345)).toBe('12,345회');
  });

  it('유한하지 않은 값은 대시로 떨어진다', () => {
    expect(fmtCalls(Number.NaN)).toBe('—');
  });
});

describe('fmtExact', () => {
  it('툴팁용 정확한 값에는 자릿수 구분만 넣는다', () => {
    expect(fmtExact(615_006)).toBe('615,006');
    expect(fmtExact(24)).toBe('24');
    expect(fmtExact(Number.NaN)).toBe('—');
  });
});
