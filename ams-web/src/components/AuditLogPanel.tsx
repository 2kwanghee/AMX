'use client';

import { useCallback, useEffect, useState } from 'react';
import { api, krApiError } from '@/lib/api-client/client';
import type { AuditLogEntry } from '@/lib/api-client/types';
import { Icon, LiveDot, TimeCell, markDataArrived } from './common';

// 감사 로그는 라이브 폴링 대상이 아니다(과거 기록의 추적). 필터·'더 보기'로만
// 새로 읽고, 폴링은 두지 않아 상황판·사용량 패널의 폴링 총량을 늘리지 않는다.
const PAGE_SIZE = 50;

// -- 날짜 유틸 ----------------------------------------------------------------
// 날짜 입력(YYYY-MM-DD)을 서버에 보낼 ISO 8601 구간으로 바꾼다. 시작은 그날
// 00:00:00Z, 종료는 그날을 포함하도록 23:59:59Z(둘 다 UTC — 서버 기준과 일치).
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

// 결과 코드 색상 — 2xx 정상, 4xx 경고(요청 오류), 5xx 심각(서버 오류).
function codeTone(status: number): string {
  if (status >= 500) return 'crit';
  if (status >= 400) return 'warn';
  return 'ok';
}

export function AuditLogPanel({ tenantId }: { tenantId: string }) {
  const [{ from, to }, setRange] = useState(defaultRange);
  const [items, setItems] = useState<AuditLogEntry[]>([]);
  const [nextToken, setNextToken] = useState<string | undefined>(undefined);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [loaded, setLoaded] = useState(false);

  // 첫 페이지(필터 변경 시 초기화) / 다음 페이지(pageToken 이어받기)를 함께 처리.
  const load = useCallback(
    async (pageToken?: string) => {
      setLoading(true);
      setError('');
      try {
        const res = await api.getAuditLogs(tenantId, {
          from: `${from}T00:00:00Z`,
          to: `${to}T23:59:59Z`,
          limit: PAGE_SIZE,
          pageToken,
        });
        const rows = res.items ?? [];
        setItems((prev) => (pageToken ? [...prev, ...rows] : rows));
        setNextToken(res.pageInfo?.nextPageToken || undefined);
        setLoaded(true);
        markDataArrived();
      } catch (e) {
        setError(krApiError(e));
      } finally {
        setLoading(false);
      }
    },
    [tenantId, from, to],
  );

  // 테넌트·기간이 바뀌면 처음부터 다시 읽는다.
  useEffect(() => {
    void load();
  }, [load]);

  const isEmpty = loaded && items.length === 0;

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>감사 로그<LiveDot /></h2>
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
        </div>
      </div>

      {error && <p className="err">{error}</p>}

      {!loaded && loading && <p className="muted">불러오는 중…</p>}
      {isEmpty && <p className="muted">이 기간에 감사 로그가 없습니다.</p>}

      {items.length > 0 && (
        <div className="table-wrap">
          <table className="usage-table">
            <thead>
              <tr>
                <th>시각</th>
                <th>관리자</th>
                <th>액션</th>
                <th>대상</th>
                <th className="num">결과</th>
              </tr>
            </thead>
            <tbody>
              {items.map((e) => (
                <tr key={e.id}>
                  <td><TimeCell iso={e.createdAt} /></td>
                  <td><span className="mono">{e.adminEmail}</span></td>
                  <td>
                    <span className="usage-server">
                      <Icon name="activity" size={14} />
                      {e.action}
                    </span>
                    <div className="muted" style={{ fontSize: 12 }}>
                      <span className="mono">{e.method} {e.path}</span>
                    </div>
                  </td>
                  <td>{e.targetId ? <span className="mono">{e.targetId}</span> : <span className="muted">—</span>}</td>
                  <td className="num">
                    <span className={`sync-cell ${codeTone(e.statusCode)}`}>{e.statusCode}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {nextToken && (
        <button
          style={{ marginTop: 12 }}
          disabled={loading}
          onClick={() => void load(nextToken)}
        >
          {loading ? '불러오는 중…' : '더 보기'}
        </button>
      )}
    </div>
  );
}
