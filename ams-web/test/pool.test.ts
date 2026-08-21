import { describe, expect, it } from 'vitest';

import {
  allowedPoolActions,
  chainStepLabel,
  coolingRemainingMs,
  fmtRemaining,
  groupAccountsByLane,
  isChainActive,
  isChainStep,
  isPoolState,
  isRecommendationKind,
  poolCounts,
  poolEventKindLabel,
  poolStateLabel,
  recommendationKindLabel,
  windowLabel,
  windowPct,
} from '@/lib/pool';
import type { PoolAccount } from '@/lib/api-client/types';

function acc(over: Partial<PoolAccount>): PoolAccount {
  return {
    accountId: 'a1',
    email: 'a@x.io',
    provider: 'claude',
    poolState: 'ready',
    windows: [],
    ...over,
  };
}

describe('라벨 매핑', () => {
  it('풀 상태를 한글 명사로 바꾼다', () => {
    expect(poolStateLabel('ready')).toBe('배급처');
    expect(poolStateLabel('leased')).toBe('대여중');
    expect(poolStateLabel('cooling')).toBe('충전소');
    expect(poolStateLabel('pinned')).toBe('고정');
    expect(poolStateLabel('held')).toBe('보류');
  });

  it('권고 종류를 계약대로 매핑한다', () => {
    expect(recommendationKindLabel('prefetch')).toBe('미리 전달');
    expect(recommendationKindLabel('swap')).toBe('교체');
    expect(recommendationKindLabel('lease')).toBe('배정');
    expect(recommendationKindLabel('recall_idle')).toBe('초과 회수');
  });

  it('체인 단계와 이벤트 종류를 매핑한다', () => {
    expect(chainStepLabel('deliver')).toBe('전달');
    expect(chainStepLabel('failed')).toBe('실패');
    expect(poolEventKindLabel('automation_paused')).toBe('자동화 정지');
    expect(poolEventKindLabel('chain_step')).toBe('체인 진행');
  });

  it('표준 창은 고정 이름, 그 밖은 식별자를 그대로 쓴다', () => {
    expect(windowLabel('five_hour')).toBe('5시간');
    expect(windowLabel('seven_day')).toBe('7일');
    expect(windowLabel('monthly')).toBe('monthly');
  });

  it('모르는 값은 원문으로 떨어진다', () => {
    expect(poolStateLabel('unknown_state')).toBe('unknown_state');
    expect(recommendationKindLabel('mystery')).toBe('mystery');
  });
});

describe('타입 가드', () => {
  it('풀 상태를 가린다', () => {
    expect(isPoolState('cooling')).toBe(true);
    expect(isPoolState('nope')).toBe(false);
    expect(isPoolState(3)).toBe(false);
  });

  it('권고 종류와 체인 단계를 가린다', () => {
    expect(isRecommendationKind('recall_idle')).toBe(true);
    expect(isRecommendationKind('deliver')).toBe(false);
    expect(isChainStep('switch')).toBe(true);
    expect(isChainStep('switch_now')).toBe(false);
  });

  it('진행 중 체인만 참으로 본다', () => {
    expect(isChainActive('deliver')).toBe(true);
    expect(isChainActive('switch')).toBe(true);
    expect(isChainActive('done')).toBe(false);
    expect(isChainActive('failed')).toBe(false);
  });
});

describe('상태별 허용 동작', () => {
  it('배급처는 고정과 보류만', () => {
    expect(allowedPoolActions('ready')).toEqual(['pin', 'hold']);
  });
  it('충전소는 해제까지 허용', () => {
    expect(allowedPoolActions('cooling')).toEqual(['pin', 'hold', 'release']);
  });
  it('고정은 해제만, 보류는 되돌리기만', () => {
    expect(allowedPoolActions('pinned')).toEqual(['unpin']);
    expect(allowedPoolActions('held')).toEqual(['release']);
  });
  it('회수중도 고정과 보류를 허용한다', () => {
    expect(allowedPoolActions('recalling')).toEqual(['pin', 'hold']);
  });
});

describe('충전소 남은 시간', () => {
  const now = Date.parse('2026-08-21T00:00:00Z');

  it('미래 시각까지 남은 밀리초를 준다', () => {
    const until = new Date(now + 90 * 60000).toISOString();
    expect(coolingRemainingMs(until, now)).toBe(90 * 60000);
  });

  it('지난 시각이나 빈 값은 0', () => {
    const past = new Date(now - 60000).toISOString();
    expect(coolingRemainingMs(past, now)).toBe(0);
    expect(coolingRemainingMs(null, now)).toBe(0);
    expect(coolingRemainingMs(undefined, now)).toBe(0);
    expect(coolingRemainingMs('not-a-date', now)).toBe(0);
  });

  it('분 단위로 끊어 읽는다', () => {
    expect(fmtRemaining(0)).toBe('복귀 대기');
    expect(fmtRemaining(30 * 1000)).toBe('1분 이내');
    expect(fmtRemaining(45 * 60000)).toBe('45분');
    expect(fmtRemaining((2 * 60 + 15) * 60000)).toBe('2시간 15분');
    expect(fmtRemaining((25 * 60) * 60000)).toBe('1일 1시간');
  });
});

describe('창 사용률 조회', () => {
  const a = acc({
    windows: [
      { windowId: 'five_hour', pct: 82, reportedAt: '2026-08-21T00:00:00Z', serverId: 's1' },
      { windowId: 'seven_day', pct: 41, reportedAt: '2026-08-21T00:00:00Z', serverId: 's1' },
    ],
  });
  it('창이 있으면 pct, 없으면 null', () => {
    expect(windowPct(a, 'five_hour')).toBe(82);
    expect(windowPct(a, 'seven_day')).toBe(41);
    expect(windowPct(a, 'monthly')).toBeNull();
  });
});

describe('보드 분류와 요약', () => {
  const accounts: PoolAccount[] = [
    acc({ accountId: 'r1', poolState: 'ready' }),
    acc({ accountId: 'l1', poolState: 'leased' }),
    acc({ accountId: 'rc1', poolState: 'recalling' }),
    acc({ accountId: 'c1', poolState: 'cooling' }),
    acc({ accountId: 'p1', poolState: 'pinned' }),
    acc({ accountId: 'h1', poolState: 'held' }),
  ];

  it('대여중 열은 leased와 recalling을 함께 담는다', () => {
    const lanes = groupAccountsByLane(accounts);
    expect(lanes.ready.map((a) => a.accountId)).toEqual(['r1']);
    expect(lanes.leased.map((a) => a.accountId)).toEqual(['l1', 'rc1']);
    expect(lanes.cooling.map((a) => a.accountId)).toEqual(['c1']);
    expect(lanes.pinned.map((a) => a.accountId)).toEqual(['p1']);
    expect(lanes.held.map((a) => a.accountId)).toEqual(['h1']);
  });

  it('요약 수치도 대여중을 합쳐 센다', () => {
    expect(poolCounts(accounts)).toEqual({ ready: 1, leased: 2, cooling: 1, pinned: 1, held: 1 });
  });
});
