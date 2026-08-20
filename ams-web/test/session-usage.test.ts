import { describe, expect, it } from 'vitest';

import {
  addCounts,
  aggregateSessionModels,
  sessionTotals,
  share,
} from '@/lib/session-usage';
import type { SessionUsageRow } from '@/lib/api-client/types';

function row(over: Partial<SessionUsageRow> = {}): SessionUsageRow {
  return {
    sessionId: 's1',
    model: 'claude-opus-5',
    accountEmail: 'khee@tscorp.ai',
    inputTokens: 100,
    outputTokens: 1000,
    cacheReadTokens: 20000,
    cacheCreate1HTokens: 5000,
    cacheCreate5MTokens: 0,
    thinkingTokens: 250,
    webSearchRequests: 1,
    webFetchRequests: 2,
    messageCount: 10,
    serviceTierCounts: { standard: 10 },
    stopReasonCounts: { tool_use: 9, end_turn: 1 },
    truncated: false,
    startedAt: '2026-08-19T09:00:00+00:00',
    endedAt: '2026-08-19T10:00:00+00:00',
    ...over,
  };
}

describe('aggregateSessionModels', () => {
  it('모델별로 접고 카운트 맵을 누적한다', () => {
    const out = aggregateSessionModels([
      row(),
      row({ sessionId: 's2', outputTokens: 500, cacheCreate5MTokens: 700 }),
      row({ sessionId: 's1', model: 'claude-sonnet-5', cacheReadTokens: 10 }),
    ]);
    expect(out.map((m) => m.model)).toEqual(['claude-opus-5', 'claude-sonnet-5']);
    const opus = out[0];
    expect(opus.sessions).toBe(2);
    expect(opus.messages).toBe(20);
    expect(opus.outputTokens).toBe(1500);
    // 1시간·5분 캐시는 각각 남는다(합쳐지지 않는다).
    expect(opus.cache1hTokens).toBe(10000);
    expect(opus.cache5mTokens).toBe(700);
    expect(opus.tierCounts).toEqual({ standard: 20 });
    expect(opus.stopCounts).toEqual({ tool_use: 18, end_turn: 2 });
  });

  it('총 토큰 내림차순으로 정렬한다', () => {
    const out = aggregateSessionModels([
      row({ model: 'small', cacheReadTokens: 1, cacheCreate1HTokens: 0, inputTokens: 0, outputTokens: 1 }),
      row({ model: 'big', cacheReadTokens: 900000 }),
    ]);
    expect(out.map((m) => m.model)).toEqual(['big', 'small']);
  });

  it('빈 입력은 빈 배열이다', () => {
    expect(aggregateSessionModels([])).toEqual([]);
  });
});

describe('sessionTotals', () => {
  it('세션 수는 중복 없는 세션 id 수다', () => {
    // 한 세션이 두 모델을 쓰면 행은 둘이지만 세션은 하나다.
    const t = sessionTotals([row(), row({ model: 'claude-sonnet-5' }), row({ sessionId: 's2' })]);
    expect(t.sessions).toBe(2);
  });

  it('max_tokens 중단과 서버 툴 호출을 뽑는다', () => {
    const t = sessionTotals([
      row({ stopReasonCounts: { max_tokens: 3, end_turn: 1 } }),
      row({ sessionId: 's2', stopReasonCounts: { end_turn: 5 } }),
    ]);
    expect(t.maxTokensStops).toBe(3);
    expect(t.serverToolCalls).toBe(6);
  });
});

describe('addCounts', () => {
  it('없는 맵과 유한하지 않은 값은 무시한다', () => {
    const into: Record<string, number> = { a: 1 };
    addCounts(into, undefined);
    addCounts(into, { a: 2, b: Number.NaN });
    expect(into).toEqual({ a: 3 });
  });
});

describe('share', () => {
  it('분모가 0이면 null이다', () => {
    expect(share(1, 0)).toBeNull();
    expect(share(250, 1000)).toBe(25);
  });
});

describe('truncated 집계', () => {
  it('모델별로 부분 집계 세션 수를 센다', () => {
    const out = aggregateSessionModels([
      row({ truncated: true }),
      row({ sessionId: 's2' }),
      row({ sessionId: 's3', model: 'claude-sonnet-5', truncated: true }),
    ]);
    expect(out.find((m) => m.model === 'claude-opus-5')!.truncatedSessions).toBe(1);
    expect(out.find((m) => m.model === 'claude-sonnet-5')!.truncatedSessions).toBe(1);
  });

  it('합계의 부분 집계 세션 수는 중복 없는 세션 id 기준이다', () => {
    // 한 세션이 두 모델 행으로 나뉘어도 부분 집계 세션은 하나다.
    const t = sessionTotals([
      row({ truncated: true }),
      row({ model: 'claude-sonnet-5', truncated: true }),
      row({ sessionId: 's2' }),
    ]);
    expect(t.sessions).toBe(2);
    expect(t.truncatedSessions).toBe(1);
  });

  it('잘리지 않은 입력에서는 0이다', () => {
    expect(sessionTotals([row(), row({ sessionId: 's2' })]).truncatedSessions).toBe(0);
  });
});
