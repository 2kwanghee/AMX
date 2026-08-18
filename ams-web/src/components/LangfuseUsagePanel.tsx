'use client';

import { useMemo, useState } from 'react';
import useSWR from 'swr';
import { api, krApiError } from '@/lib/api-client/client';
import { fmtCalls, fmtExact, fmtTokens } from '@/lib/usage-format';
import type {
  LangfuseModelRow,
  LangfuseUsage,
  LangfuseUserRow,
} from '@/lib/api-client/types';
import { Icon, LiveDot, useMarkOnData } from './common';

// Langfuse는 관측 파이프라인을 거친 실측치라 분 단위로 튀지 않는다. 비용 패널과
// 같은 느슨한 주기로 돌려 폴링 총량을 늘리지 않는다.
const POLL = 45000;

// -- 날짜 유틸 ----------------------------------------------------------------
// UI는 언제나 유효한 YYYY-MM-DD만 만든다. 기준은 서버와 같은 UTC 일자.
function toDay(d: Date): string {
  return d.toISOString().slice(0, 10);
}

function shiftDay(day: string, delta: number): string {
  const d = new Date(`${day}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + delta);
  return toDay(d);
}

// 최근 7일: 오늘 포함 7일이므로 시작은 오늘-6.
function defaultRange(): { from: string; to: string } {
  const to = toDay(new Date());
  return { from: shiftDay(to, -6), to };
}

// -- 숫자 표기 ----------------------------------------------------------------
// 표기 규칙은 lib/usage-format.ts에 있다(node 환경 단위 테스트 대상). 여기서는
// 토큰 셀마다 정확한 값을 title로 달아, 압축 표기가 가린 자릿수를 잃지 않게 한다.
function TokenCell({ value }: { value: number }) {
  return (
    <td className="num" title={fmtExact(value)}>
      {fmtTokens(value)}
    </td>
  );
}

// -- 집계 ---------------------------------------------------------------------
interface ModelAgg {
  model: string;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheCreationTokens: number;
  totalTokens: number;
  observations: number;
  days: { day: string; totalTokens: number; observations: number }[];
}

function aggregateModels(rows: LangfuseModelRow[]): ModelAgg[] {
  const byModel = new Map<string, ModelAgg>();
  for (const r of rows) {
    let m = byModel.get(r.model);
    if (!m) {
      m = {
        model: r.model,
        inputTokens: 0,
        outputTokens: 0,
        cacheReadTokens: 0,
        cacheCreationTokens: 0,
        totalTokens: 0,
        observations: 0,
        days: [],
      };
      byModel.set(r.model, m);
    }
    m.inputTokens += r.inputTokens;
    m.outputTokens += r.outputTokens;
    m.cacheReadTokens += r.cacheReadTokens;
    m.cacheCreationTokens += r.cacheCreationTokens;
    m.totalTokens += r.totalTokens;
    m.observations += r.observations;
    m.days.push({ day: r.day, totalTokens: r.totalTokens, observations: r.observations });
  }
  const out = [...byModel.values()];
  for (const m of out) m.days.sort((a, b) => a.day.localeCompare(b.day));
  // 토큰 많은 모델부터.
  out.sort((a, b) => b.totalTokens - a.totalTokens);
  return out;
}

interface UserAgg {
  userId: string;
  totalTokens: number;
  observations: number;
}

function aggregateUsers(rows: LangfuseUserRow[]): UserAgg[] {
  const byUser = new Map<string, UserAgg>();
  for (const r of rows) {
    let u = byUser.get(r.userId);
    if (!u) {
      u = { userId: r.userId, totalTokens: 0, observations: 0 };
      byUser.set(r.userId, u);
    }
    u.totalTokens += r.totalTokens;
    u.observations += r.observations;
  }
  const out = [...byUser.values()];
  out.sort((a, b) => b.totalTokens - a.totalTokens);
  return out;
}

export function LangfuseUsagePanel({ tenantId }: { tenantId: string }) {
  const [{ from, to }, setRange] = useState(defaultRange);

  const { data, error, isLoading } = useSWR<LangfuseUsage>(
    ['usage-langfuse', tenantId, from, to],
    () => api.getLangfuseUsage(tenantId, from, to),
    { refreshInterval: POLL },
  );
  useMarkOnData(data);

  const modelRows = data?.modelRows ?? [];
  const userRows = data?.userRows ?? [];

  const models = useMemo(() => aggregateModels(modelRows), [modelRows]);
  const users = useMemo(() => aggregateUsers(userRows), [userRows]);

  const totalTokens = useMemo(
    () => models.reduce((s, m) => s + m.totalTokens, 0),
    [models],
  );
  const totalObservations = useMemo(
    () => models.reduce((s, m) => s + m.observations, 0),
    [models],
  );

  const isEmpty = data != null && modelRows.length === 0 && userRows.length === 0;

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>사용량 · Langfuse 실측<LiveDot /></h2>
        <div className="actions">
          <label className="muted" style={{ fontSize: 12 }}>
            시작
            <input
              type="date"
              value={from}
              max={to}
              onChange={(e) => e.target.value && setRange((r) => ({ ...r, from: e.target.value }))}
              style={{ marginLeft: 4 }}
            />
          </label>
          <label className="muted" style={{ fontSize: 12 }}>
            종료
            <input
              type="date"
              value={to}
              min={from}
              onChange={(e) => e.target.value && setRange((r) => ({ ...r, to: e.target.value }))}
              style={{ marginLeft: 4 }}
            />
          </label>
          {data?.uiUrl && (
            <a className="linkish" href={data.uiUrl} target="_blank" rel="noreferrer noopener">
              Langfuse에서 보기 ↗
            </a>
          )}
        </div>
      </div>

      {error && <p className="err">{krApiError(error)}</p>}

      {/* 합계 카드 */}
      <div className="usage-totals">
        <div className="usage-total">
          <span className="usage-total-label">총 토큰</span>
          <span className="usage-total-value" title={fmtExact(totalTokens)}>
            {fmtTokens(totalTokens)}
          </span>
        </div>
        <div className="usage-total">
          <span className="usage-total-label">호출 수</span>
          <span className="usage-total-value">{fmtCalls(totalObservations)}</span>
        </div>
      </div>

      {isLoading && !data && <p className="muted">불러오는 중…</p>}

      {isEmpty && (
        <p className="muted">Langfuse 미구성 또는 데이터 없음</p>
      )}

      {/* 모델별 */}
      {models.length > 0 && (
        <div className="table-wrap">
          <table className="usage-table">
            <thead>
              <tr>
                <th>모델</th>
                <th className="num">입력</th>
                <th className="num">출력</th>
                {/* 읽기/쓰기로 짝을 맞춰 방향을 분명히 한다(원 지표는 각각
                    cache_read_input_tokens / cache_creation_input_tokens). */}
                <th className="num">캐시 읽기</th>
                <th className="num">캐시 쓰기</th>
                <th className="num">총 토큰</th>
                <th className="num">호출</th>
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

      {/* 계정별 */}
      {users.length > 0 && (
        <div className="table-wrap">
          <table className="usage-table">
            <thead>
              <tr>
                <th>계정</th>
                <th className="num">총 토큰</th>
                <th className="num">호출</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.userId}>
                  <td><span className="mono">{u.userId}</span></td>
                  <TokenCell value={u.totalTokens} />
                  <td className="num">{fmtCalls(u.observations)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function ModelRow({ line }: { line: ModelAgg }) {
  const [open, setOpen] = useState(false);
  const hasTrend = line.days.length > 1;
  return (
    <>
      <tr className={`usage-row ${open ? 'open' : ''}`} onClick={() => hasTrend && setOpen((v) => !v)}>
        <td>
          <span className="usage-server">
            <Icon name="zap" size={14} />
            <span className="mono">{line.model}</span>
          </span>
        </td>
        <TokenCell value={line.inputTokens} />
        <TokenCell value={line.outputTokens} />
        <TokenCell value={line.cacheReadTokens} />
        <TokenCell value={line.cacheCreationTokens} />
        <TokenCell value={line.totalTokens} />
        <td className="num">{fmtCalls(line.observations)}</td>
      </tr>
      {hasTrend && open && (
        <tr className="usage-detail-row">
          <td colSpan={7}>
            <table className="usage-accounts">
              <thead>
                <tr>
                  <th>일자</th>
                  <th className="num">총 토큰</th>
                  <th className="num">호출</th>
                </tr>
              </thead>
              <tbody>
                {line.days.map((d) => (
                  <tr key={d.day}>
                    <td><span className="mono">{d.day}</span></td>
                    <TokenCell value={d.totalTokens} />
                    <td className="num">{fmtCalls(d.observations)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </td>
        </tr>
      )}
    </>
  );
}
