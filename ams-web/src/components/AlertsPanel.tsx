'use client';

import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import type { AccountPage, AlertPage, ServerPage } from '@/lib/api-client/types';
import { Badge, LiveDot, TimeCell, krLabel, useAction, useMarkOnData } from './common';

const POLL = 7000;

// 경보 detail(JSON 객체)에서 사람이 읽을 사유 한 줄을 뽑는다. self_update_failed는
// detail 안에 error_code·detail/message를 담아 오므로 code: message로 합치고,
// 알 수 없는 형태면 통째로 JSON 문자열화한다(표에서 말줄임 + title 툴팁).
function detailText(detail?: Record<string, unknown>): string {
  if (!detail || Object.keys(detail).length === 0) return '';
  const s = (v: unknown) => (typeof v === 'string' && v.trim() ? v.trim() : undefined);
  const code = s(detail.error_code) ?? s(detail.code);
  const msg = s(detail.detail) ?? s(detail.message) ?? s(detail.reason);
  if (code && msg) return `${code}: ${msg}`;
  if (code) return code;
  if (msg) return msg;
  return JSON.stringify(detail);
}

// 경보 kind별 "사유 + 다음 행동" 한글 문구. detail은 에이전트·AMS가 남긴 영문 진단
// 문장이라 그대로 띄우면 운영자가 무엇을 해야 할지 알 수 없다 — client.ts의
// KR_API_ERROR를 도입한 것과 같은 이유다. 발생 조건을 코드에서 확인한 kind만 담는다:
// 확인하지 못한 kind는 지어낸 문구보다 detailText 폴백이 낫다. 원문 detail은 버리지
// 않고 title 툴팁으로 남긴다.
const KR_ALERT_REASON: Record<string, string> = {
  // reporter.go의 PoolSummary.AllExhausted(활성 계정 전부가 전환 임계값 이상) 또는
  // tsamx 종료코드 3. sync_from_report가 all_exhausted false인 리포트로 자동 해소한다.
  all_exhausted:
    '이 서버에 배정된 계정이 모두 사용량 한도에 닿아 전환할 대상이 없습니다. 여유가 남은 계정을 이 서버에 추가로 배정하거나, 한도가 초기화될 때까지 기다려야 합니다.',
  // grpc/server.py _mark_offline(세션 종료)과 alerts.sweep_offline(last_seen_at이
  // 하트비트 주기의 3배 경과). 하트비트나 사용량 리포트가 오면 resolve로 닫힌다.
  server_offline:
    '에이전트에서 오는 하트비트가 끊겼습니다. 세션이 닫혔거나 마지막 소식이 하트비트 주기의 세 배를 넘겼습니다. 서버에서 ama-agent가 살아 있는지, AMS로 나가는 연결이 막히지 않았는지 확인하세요. 하트비트나 사용량 리포트가 다시 들어오면 저절로 해소됩니다.',
  // reconcile.reconcile_from_report: 배정 상태(desired)와 리포트의 실제 설치 상태가
  // 어긋난 계정. 자동 교정은 두 방향(재전달·재회수)뿐이고 나머지는 알림만이다.
  drift:
    '이 계정의 서버 실제 상태가 AMS의 배정 상태와 어긋났습니다. 배정된 계정이 서버에 없으면 다시 전달하고 해제된 계정이 서버에 남아 있으면 다시 회수하는 것까지는 AMS가 스스로 맞춥니다. 그 밖의 불일치는 알림만 올라오니 배정 목록에서 상태를 직접 확인하세요.',
  // scheduler.go enqueueQuarantine: tsamx의 autoswitch_state.json에 새로 격리로
  // 올라온 계정(TRIGGER_FAILOVER)을 에이전트가 이벤트로 올린다.
  quarantine:
    '자동 전환이 이 계정을 격리해 전환 대상에서 뺐습니다. 격리된 계정은 이 서버에서 더 쓰이지 않습니다. 계정이 다시 쓸 만한 상태인지 확인한 뒤 배정 목록에서 복구를 실행하세요.',
  // commands.request_recall의 재시도 상한 초과와 reconcile.apply_ack의 회수 실패
  // (DIVERGED/REJECTED). 두 경로가 같은 dedupe 키로 모인다.
  recall_failed:
    '회수가 실패했습니다. 배정이 회수 중에 멈춰 있어 이 계정을 지우거나 다른 서버에 다시 배정하지 못합니다. 서버에서 에이전트 로그로 원인을 확인한 뒤 회수를 다시 실행하세요. 재시도 한도를 넘겼다면 전체 관리자만 강제 회수로 풀 수 있습니다.',
  // commands.py: 계정 범위 명령(deliver/activate/deactivate/switch_now)의 전송
  // 재시도가 상한까지 실패한 경우. 서버 범위 명령은 자기 치유되므로 경보가 없다.
  command_send_failed:
    '이 계정으로 보낸 명령이 전송 재시도 한도까지 실패했습니다. 전달은 대기로 되돌아가고 활성·비활성 전환은 폐기되므로, 그대로 두면 아무 일도 일어나지 않습니다. 서버가 온라인인지 확인한 뒤 배정 목록에서 같은 작업을 다시 실행하세요.',
  // §5.7 토큰 재료 가드: 에이전트가 껍데기 push를 스스로 드롭했을 때(이벤트) 또는
  // 구버전 에이전트의 push를 AMS가 거부했을 때(_apply_cred_update) 열린다.
  // 감지하는 것은 "파일에 토큰 재료가 없다"뿐이고 원인은 특정하지 않는다. 문구가
  // 원인을 단정하면 운영자가 그 하나만 확인하다 실제 원인을 놓친다 — 계정 병용은
  // 2026-08-17 사례에서 세운 가설이지 코드가 판정하는 조건이 아니다.
  credential_unusable:
    '이 계정의 자격증명 파일에 토큰이 남아 있지 않습니다. 파일이 비었다는 것만 감지되고 원인은 알 수 없습니다. 계정을 다시 인증해 자격증명을 새로 채우세요. 같은 계정을 개인 프로필에서 함께 쓰고 있다면 토큰 회전이 맞부딪친 것일 수 있으니, 계정 편집에서 배정 제외를 켜서 새 배정 대상에서 빼두세요.',
};

export function AlertsBadge({ tenantId }: { tenantId: string }) {
  const { data } = useSWR<AlertPage>(
    tenantId ? ['alerts', tenantId, 'open'] : null,
    () => api.listAlerts(tenantId, 'open'),
    { refreshInterval: POLL },
  );
  const open = data?.items?.length ?? 0;
  if (!open) return null;
  return <span className="alert-badge">{open}</span>;
}

export function AlertsPanel({ tenantId }: { tenantId: string }) {
  const { data, error, isLoading, mutate } = useSWR<AlertPage>(
    tenantId ? ['alerts', tenantId, 'all'] : null,
    () => api.listAlerts(tenantId),
    { refreshInterval: POLL },
  );
  // 대상 서버명 해석용. 신규 SWR 키를 만들지 않고 ServersPanel과 동일한
  // ['servers', tenantId] 키를 재사용해 캐시를 공유한다.
  const { data: serversData } = useSWR<ServerPage>(
    tenantId ? ['servers', tenantId] : null,
    () => api.listServers(tenantId),
    { refreshInterval: POLL },
  );
  // 대상 계정 이메일 해석용. 계정 범위 경보(drift·quarantine·recall_failed·
  // command_send_failed·credential_unusable)는 UUID만으로는 어느 계정인지 알 수 없다.
  // 서버명과 같은 방식으로 AccountsPanel의 ['accounts', tenantId] 키를 재사용한다.
  const { data: accountsData } = useSWR<AccountPage>(
    tenantId ? ['accounts', tenantId] : null,
    () => api.listAccounts(tenantId),
    { refreshInterval: POLL },
  );
  const act = useAction();
  useMarkOnData(data);
  const items = data?.items ?? [];
  const serverNameOf = new Map((serversData?.items ?? []).map((s) => [s.id, s.name]));
  const serverLabel = (id?: string) => (id ? serverNameOf.get(id) ?? id.slice(0, 8) : '—');
  const accountEmailOf = new Map((accountsData?.items ?? []).map((a) => [a.id, a.email]));
  const accountLabel = (id?: string) => (id ? accountEmailOf.get(id) ?? id.slice(0, 8) : '—');

  return (
    <div className="panel">
      <h2>알림<LiveDot /></h2>
      {error && (
        <p className="err">
          알림을 불러오지 못했습니다: {error instanceof Error ? error.message : '요청 실패'}.
        </p>
      )}
      {act.error && <p className="err">{act.error}</p>}
      {isLoading && <p className="muted">불러오는 중…</p>}
      {!isLoading && items.length === 0 && !error && <p className="muted">알림이 없습니다.</p>}
      {items.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>심각도</th>
                <th>종류</th>
                <th>대상 서버</th>
                <th>대상 계정</th>
                <th>사유</th>
                <th>상태</th>
                <th>발생 시각</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {items.map((a) => (
                <tr key={a.id}>
                  <td><Badge value={a.severity} /></td>
                  <td>{krLabel(a.kind)}</td>
                  <td>{serverLabel(a.serverId)}</td>
                  {/* 서버 범위 경보는 계정이 없다 — 다른 빈 칸과 같은 muted '—'. */}
                  <td className={a.accountId ? undefined : 'muted'}>
                    {accountLabel(a.accountId)}
                  </td>
                  <td className="muted">
                    {(() => {
                      const kr = KR_ALERT_REASON[a.kind];
                      const raw = detailText(a.detail);
                      // 한글 문구는 다음 행동까지 담아 길다. 말줄임하면 정작 조치가
                      // 잘리므로 이 경우에만 줄바꿈을 허용하고, 영문 원문은 툴팁에 남긴다.
                      if (kr) {
                        return (
                          <span
                            title={raw || undefined}
                            style={{ display: 'inline-block', maxWidth: 360, whiteSpace: 'normal' }}
                          >
                            {kr}
                          </span>
                        );
                      }
                      return raw ? (
                        <span
                          title={raw}
                          style={{ display: 'inline-block', maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', verticalAlign: 'bottom' }}
                        >
                          {raw}
                        </span>
                      ) : (
                        '—'
                      );
                    })()}
                  </td>
                  <td><Badge value={a.status} /></td>
                  <td><TimeCell iso={a.createdAt} /></td>
                  <td>
                    <button
                      disabled={a.status !== 'open' || act.busy}
                      onClick={() => act.run(() => api.ackAlert(tenantId, a.id), () => mutate())}
                    >
                      확인
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
