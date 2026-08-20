'use client';

import { useMemo, useState } from 'react';
import useSWR from 'swr';
import { api, krApiError } from '@/lib/api-client/client';
import {
  aggregateSessionModels,
  sessionTotals,
  share,
  type SessionModelAgg,
} from '@/lib/session-usage';
import { fmtCalls, fmtExact, fmtTokens } from '@/lib/usage-format';
import type { SessionUsage } from '@/lib/api-client/types';
import { Icon, LiveDot, relTime, useMarkOnData } from './common';

// 세션 단위 데이터는 세션이 끝날 때만 늘어난다. 위 두 패널과 같은 느슨한 주기로
// 돌려 폴링 총량을 늘리지 않는다.
const POLL = 45000;

const RANGES = [7, 14, 30] as const;

// 표기 규칙은 lib/usage-format.ts에 있다. 토큰 셀마다 정확한 값을 title로 달아
// 압축 표기가 가린 자릿수를 잃지 않게 한다.
function TokenCell({ value }: { value: number }) {
  return (
    <td className="num" title={fmtExact(value)}>
      {fmtTokens(value)}
    </td>
  );
}

function pct(part: number, whole: number): string {
  const value = share(part, whole);
  return value === null ? '—' : `${value.toFixed(1)}%`;
}

/** {키: 횟수}를 "키 n · 키 n" 한 줄로. 횟수 많은 쪽이 앞이다. */
function countLine(counts: Record<string, number>): string {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) return '—';
  return entries.map(([key, n]) => `${key} ${fmtExact(n)}`).join(' · ');
}

export function SessionUsagePanel({ tenantId }: { tenantId: string }) {
  const [days, setDays] = useState<number>(RANGES[0]);

  const { data, error, isLoading } = useSWR<SessionUsage>(
    ['usage-sessions', tenantId, days],
    () => api.getSessionUsage(tenantId, days),
    { refreshInterval: POLL },
  );
  useMarkOnData(data);

  const rows = data?.rows ?? [];
  const models = useMemo(() => aggregateSessionModels(rows), [rows]);
  const totals = useMemo(() => sessionTotals(rows), [rows]);

  const cacheWrite = totals.cache1hTokens + totals.cache5mTokens;
  const isEmpty = data != null && rows.length === 0;

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>사용량 · 세션 실측<LiveDot /></h2>
        <div className="actions">
          <label className="muted" style={{ fontSize: 12 }}>
            기간
            <select
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
              style={{ marginLeft: 4 }}
            >
              {RANGES.map((d) => (
                <option key={d} value={d}>{`최근 ${d}일`}</option>
              ))}
            </select>
          </label>
          {data?.lastReportedAt && (
            <span className="muted" style={{ fontSize: 12 }}>
              마지막 보고 {relTime(data.lastReportedAt)}
            </span>
          )}
        </div>
      </div>

      {error && <p className="err">{krApiError(error)}</p>}

      {/* 합계 카드 — 캐시 쓰기를 1시간/5분으로 갈라 놓는 것이 이 패널의 요점이다. */}
      <div className="usage-totals">
        <div className="usage-total">
          <span className="usage-total-label">세션</span>
          <span className="usage-total-value">{fmtExact(totals.sessions)}</span>
        </div>
        <div className="usage-total">
          <span className="usage-total-label">1시간 캐시 쓰기</span>
          <span className="usage-total-value" title={fmtExact(totals.cache1hTokens)}>
            {fmtTokens(totals.cache1hTokens)}
          </span>
          <span className="usage-total-label">캐시 쓰기의 {pct(totals.cache1hTokens, cacheWrite)}</span>
        </div>
        <div className="usage-total">
          <span className="usage-total-label">5분 캐시 쓰기</span>
          <span className="usage-total-value" title={fmtExact(totals.cache5mTokens)}>
            {fmtTokens(totals.cache5mTokens)}
          </span>
          <span className="usage-total-label">캐시 쓰기의 {pct(totals.cache5mTokens, cacheWrite)}</span>
        </div>
        <div className="usage-total">
          <span className="usage-total-label">출력 중 thinking</span>
          <span className="usage-total-value" title={fmtExact(totals.thinkingTokens)}>
            {pct(totals.thinkingTokens, totals.outputTokens)}
          </span>
          <span className="usage-total-label">
            출력 {fmtTokens(totals.outputTokens)} 중 {fmtTokens(totals.thinkingTokens)}
          </span>
        </div>
        <div className="usage-total">
          <span className="usage-total-label">서버 툴 호출</span>
          <span className="usage-total-value">{fmtExact(totals.serverToolCalls)}</span>
          <span className="usage-total-label">웹 검색·페치</span>
        </div>
        <div className="usage-total">
          <span className="usage-total-label">max_tokens 중단</span>
          <span className="usage-total-value">{fmtExact(totals.maxTokensStops)}</span>
          <span className="usage-total-label">재시도 비용의 근거</span>
        </div>
      </div>

      <p className="muted usage-note">
        1시간 캐시와 5분 캐시는 쓰기 단가가 다르다. Langfuse는 두 값을 합쳐서 보고하므로
        이 표만 둘을 구분한다. 요금 티어는 모델 행을 눌러 확인한다.
      </p>

      {/* 부분 집계 경고 — 합계가 실제보다 작다는 사실을 숨기지 않는다. */}
      {totals.truncatedSessions > 0 && (
        <p className="usage-hint">
          <Icon name="alert" size={14} />
          세션 {fmtExact(totals.truncatedSessions)}건은 훅 읽기 상한에 걸려 일부만 집계했다.
          그만큼 합계가 실제보다 작다.
        </p>
      )}

      {isLoading && !data && <p className="muted">불러오는 중…</p>}

      {isEmpty && <p className="muted">세션 훅 미설치 또는 데이터 없음</p>}

      {models.length > 0 && (
        <div className="table-wrap">
          <table className="usage-table">
            <thead>
              <tr>
                <th>모델</th>
                <th className="num">세션</th>
                <th className="num">메시지</th>
                <th className="num">입력</th>
                <th className="num">출력</th>
                <th className="num">thinking</th>
                <th className="num">캐시 읽기</th>
                <th className="num">캐시 쓰기 1시간</th>
                <th className="num">캐시 쓰기 5분</th>
                <th className="num">서버 툴</th>
              </tr>
            </thead>
            <tbody>
              {models.map((m) => (
                <ModelRow key={m.model} line={m} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function ModelRow({ line }: { line: SessionModelAgg }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <tr className={`usage-row ${open ? 'open' : ''}`} onClick={() => setOpen((v) => !v)}>
        <td>
          <span className="usage-server">
            <Icon name="zap" size={14} />
            <span className="mono">{line.model}</span>
            {line.truncatedSessions > 0 && (
              <span className="usage-flag partial" title="훅 읽기 상한에 걸린 부분 집계">
                부분 {fmtExact(line.truncatedSessions)}
              </span>
            )}
          </span>
        </td>
        <td className="num">{fmtExact(line.sessions)}</td>
        <td className="num">{fmtExact(line.messages)}</td>
        <TokenCell value={line.inputTokens} />
        <TokenCell value={line.outputTokens} />
        <td className="num" title={fmtExact(line.thinkingTokens)}>
          {pct(line.thinkingTokens, line.outputTokens)}
        </td>
        <TokenCell value={line.cacheReadTokens} />
        <TokenCell value={line.cache1hTokens} />
        <TokenCell value={line.cache5mTokens} />
        <td className="num">{fmtCalls(line.webSearchRequests + line.webFetchRequests)}</td>
      </tr>
      {open && (
        <tr className="usage-detail-row">
          <td colSpan={10}>
            <table className="usage-accounts">
              <tbody>
                <tr>
                  <td>요금 티어별 메시지</td>
                  <td className="mono">{countLine(line.tierCounts)}</td>
                </tr>
                <tr>
                  <td>종료 사유별 메시지</td>
                  <td className="mono">{countLine(line.stopCounts)}</td>
                </tr>
                <tr>
                  <td>웹 검색 · 웹 페치</td>
                  <td className="mono">
                    {fmtCalls(line.webSearchRequests)} · {fmtCalls(line.webFetchRequests)}
                  </td>
                </tr>
              </tbody>
            </table>
          </td>
        </tr>
      )}
    </>
  );
}
