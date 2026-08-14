'use client';

import { useMemo, useState } from 'react';
import useSWR from 'swr';
import { api, krApiError } from '@/lib/api-client/client';
import type {
  UsageCostAccountLine,
  UsageCostResponse,
  UsageCostServerLine,
} from '@/lib/api-client/types';
import { Icon, LiveDot, ProviderTag, fmtTime, useMarkOnData } from './common';

// 비용은 분 단위로 튀는 값이 아니다(일 단위 롤업 + 진행 중 tail). 다른 패널보다
// 느슨하게 돌려 폴링 총량을 늘리지 않는다.
const POLL = 45000;

// -- 숫자 표기 ----------------------------------------------------------------
// 서버가 Decimal을 문자열로 내려보내므로 Number로 되돌리지 않는다. 자릿수 구분만
// 문자열 위에서 넣어 표시 오차를 원천적으로 없앤다(정수부만 3자리 콤마, 소수부는
// 서버가 준 자릿수 그대로).
const DECIMAL_RE = /^-?\d+(\.\d+)?$/;

export function fmtDecimal(value?: string | null): string {
  if (value == null) return '—';
  const s = value.trim();
  if (!DECIMAL_RE.test(s)) return s;
  const neg = s.startsWith('-');
  const [intPart = '0', fracPart] = (neg ? s.slice(1) : s).split('.');
  const grouped = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return `${neg ? '-' : ''}${grouped}${fracPart ? `.${fracPart}` : ''}`;
}

function Money({ amount, currency }: { amount?: string | null; currency: string }) {
  return (
    <span className="money">
      <span className="money-cur">{currency}</span>
      {fmtDecimal(amount)}
    </span>
  );
}

// Decimal 문자열의 0 판정. 서버가 "0"·"0.00"·"0.0000" 중 무엇을 주든 같은
// 결과가 되도록 자릿수에 기대지 않는다(Number 변환도 하지 않는다).
function isZero(value?: string | null): boolean {
  return value == null || /^-?0(\.0+)?$/.test(value.trim());
}

function pct(value?: string | null): string {
  if (value == null) return '—';
  return `${fmtDecimal(value)}%`;
}

// -- 월 계산 ------------------------------------------------------------------
// UI는 언제나 유효한 YYYY-MM만 만든다(서버 쪽 pattern 검증에 걸릴 값을 보내지
// 않는다). 기준은 서버와 같은 UTC 월.
function currentMonth(): string {
  const now = new Date();
  return `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, '0')}`;
}

function shiftMonth(month: string, delta: number): string {
  const year = Number(month.slice(0, 4));
  const mon = Number(month.slice(5, 7));
  const zero = year * 12 + (mon - 1) + delta;
  return `${String(Math.floor(zero / 12)).padStart(4, '0')}-${String((zero % 12) + 1).padStart(2, '0')}`;
}

function krMonth(month: string): string {
  return `${month.slice(0, 4)}년 ${Number(month.slice(5, 7))}월`;
}

// 계정 가격이 이 서버에 놓인 근거. no_price는 사용자가 직접 고칠 수 있는
// 상태이므로 표에서 안내로 승격한다.
const BASIS_LABEL: Record<string, string> = {
  held: '보유 시간 기준',
  observed: '관측 시간 기준',
  unallocated: '미배분',
  no_price: '가격 미설정',
};

export function UsageCostPanel({
  tenantId,
  onGoAccounts,
}: {
  tenantId: string;
  onGoAccounts?: () => void;
}) {
  // 최초 마운트 시점의 현재 월. 이후 사용자의 이동만 반영한다.
  const [thisMonth] = useState(currentMonth);
  const [month, setMonth] = useState(thisMonth);
  const [open, setOpen] = useState<Record<string, boolean>>({});

  const { data, error, isLoading } = useSWR<UsageCostResponse>(
    ['usage-cost', tenantId, month],
    () => api.getUsageCost(tenantId, month),
    { refreshInterval: POLL },
  );
  useMarkOnData(data);

  const servers = data?.servers ?? [];
  const subtotals = data?.subtotals ?? [];
  const noPriceCount = useMemo(
    () =>
      new Set(
        servers.flatMap((s) =>
          s.accounts.filter((a) => a.basis === 'no_price').map((a) => a.accountId),
        ),
      ).size,
    [servers],
  );

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>사용량 · 비용 배분<LiveDot /></h2>
        <div className="actions month-nav">
          <button
            aria-label="이전 달"
            onClick={() => setMonth((m) => shiftMonth(m, -1))}
          >
            ‹
          </button>
          <span className="month-label">{krMonth(month)}</span>
          <button
            aria-label="다음 달"
            disabled={month >= thisMonth}
            onClick={() => setMonth((m) => (m >= thisMonth ? m : shiftMonth(m, 1)))}
          >
            ›
          </button>
          {month !== thisMonth && (
            <button onClick={() => setMonth(thisMonth)}>이번 달</button>
          )}
        </div>
      </div>

      {error && <p className="err">{krApiError(error)}</p>}

      <div className="usage-meta">
        {data?.isPartial && (
          <span className="usage-flag partial">
            <Icon name="clock" size={13} />
            진행 중 · 기준 {fmtTime(data.asOf)}
          </span>
        )}
        {data && !data.isPartial && (
          <span className="usage-flag sealed">
            <Icon name="check" size={13} />
            확정 · 기준 {fmtTime(data.asOf)}
          </span>
        )}
        {data?.watermark && (
          <span className="muted usage-note">{data.watermark} 이전 구간 확정</span>
        )}
        {data && !data.watermark && (
          <span className="muted usage-note">확정 구간 없음(집계 대기)</span>
        )}
      </div>

      {/* 통화별 총액 — 통화가 다른 금액은 절대 합치지 않는다. */}
      <div className="usage-totals">
        {subtotals.map((s) => (
          <div className="usage-total" key={s.currency}>
            <span className="usage-total-label">{s.currency} 배분 합계</span>
            <span className="usage-total-value">
              <Money amount={s.allocatedCost} currency={s.currency} />
            </span>
            {!isZero(s.unallocatedCost) && (
              <span className="usage-total-sub" title="서버에 놓을 수 없어 배분되지 않은 금액">
                미배분 <Money amount={s.unallocatedCost} currency={s.currency} />
              </span>
            )}
          </div>
        ))}
        {data && subtotals.length === 0 && (
          <p className="muted">{krMonth(month)}에 청구된 계정이 없습니다.</p>
        )}
      </div>

      {noPriceCount > 0 && (
        <p className="usage-hint">
          <Icon name="alert" size={14} />
          월 구독료가 없는 계정 {noPriceCount}개는 배분 금액이 0으로 잡힙니다.
          {onGoAccounts && (
            <button className="linkish" onClick={onGoAccounts}>계정 메뉴에서 가격 입력</button>
          )}
        </p>
      )}

      <div className="table-wrap">
        <table className="usage-table">
          <thead>
            <tr>
              <th style={{ width: 28 }} />
              <th>서버</th>
              <th className="num">사용률</th>
              <th className="num">배분 비용</th>
              <th className="num">계정</th>
            </tr>
          </thead>
          <tbody>
            {servers.map((s) => (
              <ServerRows
                key={s.serverId}
                line={s}
                open={!!open[s.serverId]}
                onToggle={() =>
                  setOpen((prev) => ({ ...prev, [s.serverId]: !prev[s.serverId] }))
                }
                onGoAccounts={onGoAccounts}
              />
            ))}
            {isLoading && !data && (
              <tr><td colSpan={5} className="muted">불러오는 중…</td></tr>
            )}
            {data && servers.length === 0 && (
              <tr>
                <td colSpan={5} className="muted">
                  {krMonth(month)}에 기록된 사용 서버가 없습니다.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ServerRows({
  line,
  open,
  onToggle,
  onGoAccounts,
}: {
  line: UsageCostServerLine;
  open: boolean;
  onToggle: () => void;
  onGoAccounts?: () => void;
}) {
  const name = line.name || line.serverId.slice(0, 8);
  return (
    <>
      <tr className={`usage-row ${open ? 'open' : ''}`} onClick={onToggle}>
        <td>
          <button
            className="row-toggle"
            aria-expanded={open}
            aria-label={`${name} 계정 상세`}
            onClick={(e) => { e.stopPropagation(); onToggle(); }}
          >
            <span className={`caret ${open ? 'open' : ''}`} aria-hidden="true">›</span>
          </button>
        </td>
        <td>
          <span className="usage-server">
            <Icon name="server" size={15} />
            {name}
          </span>
        </td>
        <td className="num">{pct(line.utilizationPct)}</td>
        <td className="num">
          {line.costs.length === 0 && <span className="muted">—</span>}
          {line.costs.map((c) => (
            <div key={c.currency}><Money amount={c.amount} currency={c.currency} /></div>
          ))}
        </td>
        <td className="num">{line.accounts.length}</td>
      </tr>
      {open && (
        <tr className="usage-detail-row">
          <td />
          <td colSpan={4}>
            <table className="usage-accounts">
              <thead>
                <tr>
                  <th>계정</th>
                  <th>프로바이더</th>
                  <th className="num">월 구독료</th>
                  <th className="num">사용률</th>
                  <th className="num">분담률</th>
                  <th className="num">배분액</th>
                </tr>
              </thead>
              <tbody>
                {line.accounts.map((a) => (
                  <AccountRow key={a.accountId} line={a} onGoAccounts={onGoAccounts} />
                ))}
                {line.accounts.length === 0 && (
                  <tr><td colSpan={6} className="muted">이 서버에 놓인 계정이 없습니다.</td></tr>
                )}
              </tbody>
            </table>
          </td>
        </tr>
      )}
    </>
  );
}

function AccountRow({
  line,
  onGoAccounts,
}: {
  line: UsageCostAccountLine;
  onGoAccounts?: () => void;
}) {
  const noPrice = line.basis === 'no_price' || line.monthlyPrice == null;
  return (
    <tr>
      <td>
        <span className="mono">{line.email || line.accountId.slice(0, 8)}</span>
      </td>
      <td>{line.provider ? <ProviderTag value={line.provider} /> : <span className="muted">—</span>}</td>
      <td className="num">
        {noPrice ? (
          <span className="usage-noprice">
            가격 미설정
            {onGoAccounts && (
              <button className="linkish" onClick={onGoAccounts}>입력</button>
            )}
          </span>
        ) : (
          <Money amount={line.monthlyPrice} currency={line.currency} />
        )}
      </td>
      <td className="num">{pct(line.utilizationPct)}</td>
      <td className="num">{pct(line.sharePct)}</td>
      <td className="num">
        <Money amount={line.cost} currency={line.currency} />
        <span className="usage-basis">{BASIS_LABEL[line.basis] ?? line.basis}</span>
      </td>
    </tr>
  );
}
