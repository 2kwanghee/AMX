'use client';

import { Fragment } from 'react';
import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import type { Server, UsageSnapshot } from '@/lib/api-client/types';
import { poolAllExhausted, poolMaxUtilization, usageAccountView } from '@/lib/usage-format';
import { Badge, Icon, Modal, fmtTime, krLabel, providerIcon } from '../common';
import { POLL } from './constants';

// Derive a human window label from its span (P2b). claude's canonical windows
// get fixed labels; anything else falls back to a minutes/hours/days rule, or to
// the provider-local id when the span is unknown.
function windowLabel(windowMinutes: number | undefined, id: string): string {
  if (windowMinutes === undefined) return id;
  if (windowMinutes === 300) return '5시간';
  if (windowMinutes === 10080) return '7일';
  if (windowMinutes >= 1440 && windowMinutes % 1440 === 0) return `${windowMinutes / 1440}일`;
  if (windowMinutes >= 60 && windowMinutes % 60 === 0) return `${windowMinutes / 60}시간`;
  return `${windowMinutes}분`;
}

// 창 id는 프로바이더가 정한다 — claude는 five_hour/seven_day, codex는
// primary/secondary(ama-agent/internal/provider/codex/bridge.go). 라벨은 창
// 길이에서 나오므로 codex의 10080분 창이 claude의 "7일"과 글자 그대로 같아진다.
// 한 화면에 두 프로바이더가 섞일 때 어느 쪽 창인지 구분되도록 id로 출처를
// 되짚어 태그를 붙인다. 모르는 id는 태그 없이 둔다(잘못 단정하지 않는다).
const WINDOW_PROVIDER: Record<string, string> = {
  five_hour: 'claude',
  seven_day: 'claude',
  primary: 'codex',
  secondary: 'codex',
};

function windowProvider(id: string): string | undefined {
  return WINDOW_PROVIDER[id];
}

export function UsageModal({
  tenantId,
  server,
  onClose,
}: {
  tenantId: string;
  server: Server;
  onClose: () => void;
}) {
  const { data, error } = useSWR<UsageSnapshot>(
    ['usage', tenantId, server.id],
    () => api.getUsage(tenantId, server.id),
    { refreshInterval: POLL },
  );
  const p = data?.payload;
  return (
    <Modal title={`사용량 — ${server.name}`} onClose={onClose}>
      {error && <p className="muted">아직 사용량 보고가 없습니다.</p>}
      {p && (
        <>
          <p>
            풀 최대 사용률: <b>{poolMaxUtilization(p) ?? 0}%</b>{' '}
            {poolAllExhausted(p) && <Badge value="critical" />}
          </p>
          <p className="muted">보고 {fmtTime(data?.reportedAt)}</p>
          <div className="table-wrap">
            <table>
              <thead><tr><th>이메일</th><th>상태</th><th>현재</th></tr></thead>
              <tbody>
                {(p.accounts ?? []).map((a, i) => {
                  const acct = usageAccountView(a);
                  return (
                    <Fragment key={acct.amsAccountId ?? i}>
                      <tr>
                        <td>{acct.email ?? '—'}</td>
                        <td>{acct.allocationStatus && <Badge value={acct.allocationStatus} />}</td>
                        <td>{acct.isCurrent ? '★' : ''}</td>
                      </tr>
                      {acct.windows.length > 0 && (
                        <tr className="usage-windows-row">
                          <td colSpan={3}>
                            <div className="usage-windows">
                              {acct.windows.map((w) => {
                                const prov = windowProvider(w.id);
                                return (
                                  <div className="usage-window" key={w.key}>
                                    {prov && (
                                      <span className={`uw-prov ${prov}`} title={`${krLabel(prov)} 창 ${w.id}`}>
                                        <Icon name={providerIcon(prov)} size={10} />
                                        {krLabel(prov)}
                                      </span>
                                    )}
                                    <span className="uw-label">{windowLabel(w.windowMinutes, w.id)}</span>
                                    <span className="uw-pct">{w.pct ?? 0}%</span>
                                  </div>
                                );
                              })}
                            </div>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Modal>
  );
}
