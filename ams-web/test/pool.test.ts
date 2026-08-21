import { describe, expect, it } from 'vitest';

import {
  allowedPoolActions,
  chainStepLabel,
  coolingProgress,
  coolingRemainingMs,
  diffChanged,
  fmtElapsed,
  fmtRemaining,
  fmtRemainingPrecise,
  groupAccountsByLane,
  isChainActive,
  isChainStep,
  isPoolState,
  isRecommendationKind,
  poolCounts,
  poolEventKindLabel,
  poolStateLabel,
  recommendationBasis,
  recommendationKindLabel,
  ineligibleReasonLabel,
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
  it('고정은 해제와 보류, 보류는 되돌리기만', () => {
    expect(allowedPoolActions('pinned')).toEqual(['unpin', 'hold']);
    expect(allowedPoolActions('held')).toEqual(['release']);
  });
  it('대여중과 회수중은 보류만 허용한다', () => {
    expect(allowedPoolActions('leased')).toEqual(['hold']);
    expect(allowedPoolActions('recalling')).toEqual(['hold']);
  });
});

describe('단계 경과 시간', () => {
  it('1분 미만은 방금, 그 이상은 분·시간으로 끊는다', () => {
    expect(fmtElapsed(0)).toBe('방금');
    expect(fmtElapsed(30 * 1000)).toBe('방금');
    expect(fmtElapsed(-1000)).toBe('방금');
    expect(fmtElapsed(5 * 60000)).toBe('5분');
    expect(fmtElapsed((2 * 60 + 15) * 60000)).toBe('2시간 15분');
    expect(fmtElapsed(25 * 60 * 60000)).toBe('1일 1시간');
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

  it('10분 미만은 mm:ss, 이상은 분 단위, 0 이하는 복귀 대기', () => {
    expect(fmtRemainingPrecise(0)).toBe('복귀 대기');
    expect(fmtRemainingPrecise(-5)).toBe('복귀 대기');
    expect(fmtRemainingPrecise((3 * 60 + 9) * 1000)).toBe('03:09');
    expect(fmtRemainingPrecise(10 * 60000 - 1000)).toBe('09:59');
    expect(fmtRemainingPrecise(10 * 60000)).toBe('10분');
    expect(fmtRemainingPrecise((1 * 3600 + 12 * 60) * 1000)).toBe('1시간 12분');
  });
});

describe('충전 진행률', () => {
  const start = '2026-08-22T00:00:00Z';
  const end = '2026-08-22T01:00:00Z';
  const t = (iso: string) => new Date(iso).getTime();

  it('시각이 하나라도 없으면 null', () => {
    expect(coolingProgress({ poolStateChangedAt: null, coolingUntil: end }, t(start))).toBeNull();
    expect(coolingProgress({ poolStateChangedAt: start, coolingUntil: undefined }, t(start))).toBeNull();
  });
  it('파싱 실패(NaN)면 null', () => {
    expect(coolingProgress({ poolStateChangedAt: 'bad', coolingUntil: end }, t(start))).toBeNull();
  });
  it('완료가 시작보다 앞이거나 같으면 null', () => {
    expect(coolingProgress({ poolStateChangedAt: end, coolingUntil: start }, t(end))).toBeNull();
    expect(coolingProgress({ poolStateChangedAt: start, coolingUntil: start }, t(start))).toBeNull();
  });
  it('구간 안에서는 비율, 지나면 1, 이전이면 0', () => {
    expect(coolingProgress({ poolStateChangedAt: start, coolingUntil: end }, t('2026-08-22T00:30:00Z'))).toBeCloseTo(0.5);
    expect(coolingProgress({ poolStateChangedAt: start, coolingUntil: end }, t('2026-08-22T02:00:00Z'))).toBe(1);
    expect(coolingProgress({ poolStateChangedAt: start, coolingUntil: end }, t('2026-08-21T00:00:00Z'))).toBe(0);
  });
});

describe('권고 판정 근거', () => {
  const policy = { swapAtPct: 85, prefetchAtPct: 70, targetLeases: 2 };
  it('교체·미리 전달은 임계 대 현재값', () => {
    expect(recommendationBasis({ kind: 'swap', triggerPct: 91.4 }, policy, 1)).toBe('교체 임계 85% 이상 · 현재 91%');
    expect(recommendationBasis({ kind: 'prefetch', triggerPct: 72 }, policy, 1)).toBe('미리 전달 임계 70% 이상 · 현재 72%');
  });
  it('배정·초과 회수는 목표 대여 대 현재 대여를 기준으로만 적는다', () => {
    expect(recommendationBasis({ kind: 'lease', triggerPct: null }, policy, 1)).toBe('기준 목표 대여 2 · 현재 대여 1');
    expect(recommendationBasis({ kind: 'recall_idle' }, policy, 3)).toBe('기준 목표 대여 2 · 현재 대여 3');
  });
  it('정책이 없으면 현재값만', () => {
    expect(recommendationBasis({ kind: 'swap', triggerPct: 90 }, undefined, 0)).toBe('현재 90%');
    expect(recommendationBasis({ kind: 'swap' }, undefined, 0)).toBe('현재값 없음');
  });
});

describe('id 집합 차이', () => {
  it('동일 집합은 빈 결과', () => {
    expect(diffChanged(['a', 'b'], ['b', 'a'])).toEqual({ added: [], removed: [] });
  });
  it('추가와 삭제를 나눈다', () => {
    expect(diffChanged(['a'], ['a', 'b'])).toEqual({ added: ['b'], removed: [] });
    expect(diffChanged(['a', 'b'], ['b'])).toEqual({ added: [], removed: ['a'] });
    expect(diffChanged(new Set(['a']), new Set(['c']))).toEqual({ added: ['c'], removed: ['a'] });
  });
});

describe('창 사용률 조회', () => {
  const a = acc({
    windows: [
      { windowId: 'five_hour', pct: 82, reportedAt: '2026-08-21T00:00:00Z', serverId: 's1' },
      { windowId: 'seven_day', pct: 41, reportedAt: '2026-08-21T00:00:00Z', serverId: 's1' },
      { windowId: 'monthly', pct: null, reportedAt: '2026-08-21T00:00:00Z', serverId: 's1' },
    ],
  });
  it('창이 있으면 pct, 없으면 null', () => {
    expect(windowPct(a, 'five_hour')).toBe(82);
    expect(windowPct(a, 'seven_day')).toBe(41);
    expect(windowPct(a, 'absent')).toBeNull();
  });
  it('관측이 없는 창(pct null)은 null을 그대로 준다', () => {
    expect(windowPct(a, 'monthly')).toBeNull();
  });
});

describe('부적격 사유 라벨', () => {
  it('사유를 짧은 한글로 바꾼다', () => {
    expect(ineligibleReasonLabel('api_key')).toBe('API 키');
    expect(ineligibleReasonLabel('excluded')).toBe('배정 제외');
    expect(ineligibleReasonLabel('no_observation')).toBe('관측 없음');
  });
  it('모르는 값은 원문으로 떨어진다', () => {
    expect(ineligibleReasonLabel('mystery')).toBe('mystery');
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
