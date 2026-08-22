import { describe, expect, it } from 'vitest';

import {
  accountWindows,
  allocationStatusLabel,
  fmtCalls,
  fmtExact,
  fmtRemainingWindow,
  fmtResetClock,
  fmtTokens,
  poolAllExhausted,
  poolMaxUtilization,
  usageAccountView,
} from '@/lib/usage-format';
import type { AccountUsageWindowSummary, UsageAccount, UsagePayload } from '@/lib/api-client/types';

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

// 서버가 실제로 저장하는 형태: proto UsageReport 를
// MessageToDict(preserving_proto_field_name=True) 로 직렬화한 snake_case dict.
// 기본값(false·0·빈)은 키가 생략된다(is_current false, all_exhausted false 등).
const STORED_PAYLOAD: UsagePayload = {
  schema_version: 1,
  agent_id: 'agent-1',
  trigger: 'TRIGGER_SCHEDULE',
  active_account: { ams_account_id: 'acc-1', email: 'a@x.io' },
  pool_summary: {
    total: 2,
    active: 2,
    eligible: 1,
    max_utilization_pct: 92,
    // all_exhausted: false 는 MessageToDict 가 생략 → 키 없음
  },
  accounts: [
    {
      account: { ams_account_id: 'acc-1', email: 'a@x.io' },
      allocation_status: 'ALLOCATION_STATUS_ACTIVE',
      is_current: true,
      windows: [
        { id: 'five_hour', pct: 92, window_minutes: 300 },
        { id: 'seven_day', pct: 40, window_minutes: 10080 },
      ],
    },
    {
      // windows[] 없음 → legacy 위치형 창으로 폴백. is_current 생략(=false).
      account: { ams_account_id: 'acc-2', email: 'b@x.io' },
      allocation_status: 'ALLOCATION_STATUS_QUARANTINED',
      five_hour: { pct: 100 },
      seven_day: { pct: 88 },
    },
  ],
};

describe('poolMaxUtilization / poolAllExhausted', () => {
  it('snake_case pool_summary 키를 읽는다', () => {
    expect(poolMaxUtilization(STORED_PAYLOAD)).toBe(92);
    expect(poolAllExhausted(STORED_PAYLOAD)).toBe(false);
  });

  it('all_exhausted 는 명시적 true 만 참으로 본다', () => {
    expect(poolAllExhausted({ pool_summary: { all_exhausted: true } })).toBe(true);
    expect(poolAllExhausted({ pool_summary: {} })).toBe(false);
  });

  it('payload 나 pool_summary 가 없으면 undefined·false 로 떨어진다', () => {
    expect(poolMaxUtilization(undefined)).toBeUndefined();
    expect(poolAllExhausted(undefined)).toBe(false);
  });
});

describe('allocationStatusLabel', () => {
  it('proto enum 이름을 소문자 상태어로 줄인다', () => {
    expect(allocationStatusLabel('ALLOCATION_STATUS_ACTIVE')).toBe('active');
    expect(allocationStatusLabel('ALLOCATION_STATUS_QUARANTINED')).toBe('quarantined');
    expect(allocationStatusLabel(undefined)).toBe('');
  });
});

describe('accountWindows', () => {
  it('windows[] 가 있으면 그대로 매핑한다', () => {
    const ws = accountWindows(STORED_PAYLOAD.accounts![0]);
    expect(ws).toEqual([
      { key: 'five_hour', id: 'five_hour', pct: 92, windowMinutes: 300 },
      { key: 'seven_day', id: 'seven_day', pct: 40, windowMinutes: 10080 },
    ]);
  });

  it('windows[] 가 없으면 five_hour/seven_day 로 폴백하고 창 길이를 채운다', () => {
    const ws = accountWindows(STORED_PAYLOAD.accounts![1]);
    expect(ws).toEqual([
      { key: 'five_hour', id: 'five_hour', pct: 100, windowMinutes: 300 },
      { key: 'seven_day', id: 'seven_day', pct: 88, windowMinutes: 10080 },
    ]);
  });

  it('창 정보가 전혀 없으면 빈 배열', () => {
    const bare: UsageAccount = { account: { ams_account_id: 'x' } };
    expect(accountWindows(bare)).toEqual([]);
  });
});

describe('usageAccountView', () => {
  it('중첩된 account.ams_account_id·email 과 정규화 상태를 읽는다', () => {
    const v = usageAccountView(STORED_PAYLOAD.accounts![0]);
    expect(v.amsAccountId).toBe('acc-1');
    expect(v.email).toBe('a@x.io');
    expect(v.allocationStatus).toBe('active');
    expect(v.isCurrent).toBe(true);
    expect(v.windows).toHaveLength(2);
  });

  it('is_current 생략은 false 로 본다', () => {
    const v = usageAccountView(STORED_PAYLOAD.accounts![1]);
    expect(v.isCurrent).toBe(false);
    expect(v.allocationStatus).toBe('quarantined');
  });
});

describe('fmtResetClock', () => {
  it('24시간제 HH:MM 로 떨어진다(오전/오후 없음)', () => {
    expect(fmtResetClock('2026-08-22T14:30:00Z')).toMatch(/^\d{2}:\d{2}$/);
  });

  it('미상·파싱 불가는 undefined', () => {
    expect(fmtResetClock(undefined)).toBeUndefined();
    expect(fmtResetClock(null)).toBeUndefined();
    expect(fmtResetClock('not-a-date')).toBeUndefined();
  });
});

describe('fmtRemainingWindow', () => {
  it('pct 를 100에서 뺀 잔여율과 리셋 시각을 병기한다', () => {
    const w: AccountUsageWindowSummary = { pct: 62, resetsAt: '2026-08-22T05:30:00Z' };
    const text = fmtRemainingWindow(w);
    expect(text).toMatch(/^잔여 38% · \d{2}:\d{2} 리셋$/);
  });

  it('리셋 시각이 없으면 리셋 문구를 뺀다', () => {
    expect(fmtRemainingWindow({ pct: 20, resetsAt: null })).toBe('잔여 80%');
  });

  it('pct 미상은 대시', () => {
    expect(fmtRemainingWindow({ pct: null, resetsAt: null })).toBe('—');
    expect(fmtRemainingWindow(null)).toBe('—');
    expect(fmtRemainingWindow(undefined)).toBe('—');
  });

  it('경계값을 0~100 사이로 자른다', () => {
    expect(fmtRemainingWindow({ pct: -5, resetsAt: null })).toBe('잔여 100%');
    expect(fmtRemainingWindow({ pct: 130, resetsAt: null })).toBe('잔여 0%');
  });
});
