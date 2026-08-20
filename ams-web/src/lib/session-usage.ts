// 세션 실측 비용구조의 집계 — SessionUsagePanel에서 떼어내 node 테스트 환경에서
// 단위 테스트가 되게 한다(DOM·React 불필요) — usage-format.ts와 같은 이유다.
//
// 서버는 (세션, 모델) 행을 그대로 준다. 화면이 필요한 것은 두 가지 축이다:
// 모델별 합계(비용 구조 비교)와 전체 합계(패널 상단 카드).

import type { SessionUsageRow } from './api-client/types';

export interface SessionModelAgg {
  model: string;
  sessions: number;
  messages: number;
  inputTokens: number;
  outputTokens: number;
  thinkingTokens: number;
  cacheReadTokens: number;
  cache1hTokens: number;
  cache5mTokens: number;
  webSearchRequests: number;
  webFetchRequests: number;
  tierCounts: Record<string, number>;
  stopCounts: Record<string, number>;
  // 이 모델의 행 중 부분 집계(훅 읽기 상한에 걸림)인 세션 수.
  truncatedSessions: number;
}

/** {키: 횟수} 맵을 누적한다. 원본은 건드리지 않는다. */
export function addCounts(
  into: Record<string, number>,
  from: Record<string, number> | undefined,
): Record<string, number> {
  if (!from) return into;
  for (const [key, n] of Object.entries(from)) {
    if (!Number.isFinite(n)) continue;
    into[key] = (into[key] ?? 0) + n;
  }
  return into;
}

function empty(model: string): SessionModelAgg {
  return {
    model,
    sessions: 0,
    messages: 0,
    inputTokens: 0,
    outputTokens: 0,
    thinkingTokens: 0,
    cacheReadTokens: 0,
    cache1hTokens: 0,
    cache5mTokens: 0,
    webSearchRequests: 0,
    webFetchRequests: 0,
    tierCounts: {},
    stopCounts: {},
    truncatedSessions: 0,
  };
}

/**
 * 모델별로 접는다. 한 세션이 모델을 섞으므로(주 모델 + 서브에이전트) 같은 세션이
 * 여러 모델 줄에 나타날 수 있고, 모델별 `sessions`는 그 모델을 쓴 세션 수다 —
 * 모델 간 합이 전체 세션 수보다 클 수 있다는 뜻이다.
 *
 * 정렬은 캐시를 포함한 총 토큰 내림차순이다.
 */
export function aggregateSessionModels(rows: SessionUsageRow[]): SessionModelAgg[] {
  const byModel = new Map<string, SessionModelAgg>();
  for (const r of rows) {
    let m = byModel.get(r.model);
    if (!m) {
      m = empty(r.model);
      byModel.set(r.model, m);
    }
    m.sessions += 1;
    m.messages += r.messageCount;
    m.inputTokens += r.inputTokens;
    m.outputTokens += r.outputTokens;
    m.thinkingTokens += r.thinkingTokens;
    m.cacheReadTokens += r.cacheReadTokens;
    m.cache1hTokens += r.cacheCreate1HTokens;
    m.cache5mTokens += r.cacheCreate5MTokens;
    m.webSearchRequests += r.webSearchRequests;
    m.webFetchRequests += r.webFetchRequests;
    addCounts(m.tierCounts, r.serviceTierCounts);
    addCounts(m.stopCounts, r.stopReasonCounts);
    if (r.truncated) m.truncatedSessions += 1;
  }
  const out = [...byModel.values()];
  out.sort(
    (a, b) =>
      b.inputTokens + b.outputTokens + b.cacheReadTokens + b.cache1hTokens + b.cache5mTokens -
      (a.inputTokens + a.outputTokens + a.cacheReadTokens + a.cache1hTokens + a.cache5mTokens),
  );
  return out;
}

export interface SessionTotals {
  sessions: number;
  cache1hTokens: number;
  cache5mTokens: number;
  outputTokens: number;
  thinkingTokens: number;
  serverToolCalls: number;
  maxTokensStops: number;
  tierCounts: Record<string, number>;
  // 부분 집계 세션 수(중복 없는 세션 id 기준). 0이 아니면 합계가 실제보다 작다.
  truncatedSessions: number;
}

/**
 * 패널 상단 카드용 전체 합계. `sessions`는 **서로 다른 세션 id의 수**다(모델별 줄을
 * 그대로 세면 한 세션이 여러 번 잡힌다).
 *
 * `maxTokensStops`는 `stop_reason == "max_tokens"`로 끊긴 메시지 수다 — 재시도 비용의
 * 직접 근거라 따로 뽑는다.
 */
export function sessionTotals(rows: SessionUsageRow[]): SessionTotals {
  const ids = new Set<string>();
  const totals: SessionTotals = {
    sessions: 0,
    cache1hTokens: 0,
    cache5mTokens: 0,
    outputTokens: 0,
    thinkingTokens: 0,
    serverToolCalls: 0,
    maxTokensStops: 0,
    tierCounts: {},
    truncatedSessions: 0,
  };
  const truncatedIds = new Set<string>();
  for (const r of rows) {
    ids.add(r.sessionId);
    if (r.truncated) truncatedIds.add(r.sessionId);
    totals.cache1hTokens += r.cacheCreate1HTokens;
    totals.cache5mTokens += r.cacheCreate5MTokens;
    totals.outputTokens += r.outputTokens;
    totals.thinkingTokens += r.thinkingTokens;
    totals.serverToolCalls += r.webSearchRequests + r.webFetchRequests;
    totals.maxTokensStops += r.stopReasonCounts?.max_tokens ?? 0;
    addCounts(totals.tierCounts, r.serviceTierCounts);
  }
  totals.sessions = ids.size;
  totals.truncatedSessions = truncatedIds.size;
  return totals;
}

/** 분모가 0이면 null(비중을 표시하지 않는다는 뜻). 아니면 0~100 백분율. */
export function share(part: number, whole: number): number | null {
  if (!Number.isFinite(part) || !Number.isFinite(whole) || whole <= 0) return null;
  return (part / whole) * 100;
}
