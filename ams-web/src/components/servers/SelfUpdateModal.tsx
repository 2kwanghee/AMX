'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import type { SelfUpdateStatus, Server } from '@/lib/api-client/types';
import { Modal, useAction, useNow } from '../common';

// SelfUpdate 2단계 — 에이전트를 최신 코드로 자체 업데이트하도록 명령한다. 에이전트는
// % 진행률을 보고하지 않으므로 단계는 명령 상태(queued→sent→acked/failed)와 servers
// 폴링에서 파생한다: 실행 직후 self-update-status를 2초 주기로 폴링(모달 로컬 SWR 키)
// 하고, acked 이후 servers 폴링에서 lastSeenAt/agentVersion이 바뀌면 완료로 본다. 진행
// 바는 단계 기반(20/40/60/80/100%)이며 연속 %는 만들지 않는다. 실패 시 detail의
// error_code를 노출하고 self_update_failed 경보가 AlertsPanel에 뜬다. 모달을 닫아도
// 무해하다 — 상태는 서버에서 재파생되고, 다시 열면 실행 버튼부터 시작한다.
// 커밋 핀(expected_commit)은 콘솔에서 보내지 않는다: 본문 없이 POST하므로 서버가 빈
// 문자열로 채우고, 빈 값은 "설정된 upstream tip"을 뜻한다. 소스 모드는 git upstream
// tip, 패키지 모드는 매니페스트 tip으로 각각 해석하므로 두 모드 모두 수용한다(다만
// 패키지 모드는 builtAt 스탬프가 없으면 롤백 기준이 없어 거부한다).
const SU_STAGES = ['명령 전송됨', '에이전트 수신', '적용 중 · pull·빌드·교체', '적용 완료 · 재시작 중', '완료'];
const SU_DELAY_MS = 10 * 60 * 1000;

function fmtElapsed(ms: number) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return s < 60 ? `${s}초` : `${Math.floor(s / 60)}분 ${s % 60}초`;
}

export function SelfUpdateModal({
  tenantId,
  server,
  onClose,
}: {
  tenantId: string;
  server: Server;
  onClose: () => void;
}) {
  const act = useAction();
  const [sent, setSent] = useState(false);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const now = useNow(1000);

  const { data: st } = useSWR<SelfUpdateStatus>(
    sent ? ['self-update-status', tenantId, server.id] : null,
    () => api.getSelfUpdateStatus(tenantId, server.id),
    { refreshInterval: 2000 },
  );

  const status = st?.status ?? null;
  const failed = status === 'failed';
  // Stage 5 (완료): ack 이후의 하트비트(lastSeenAt > ackedAt)만이 exec 재시작 후
  // 재접속을 증명한다. lastSeenAt은 평상시에도 매 하트비트마다 갱신되므로
  // "전송 시점과 다르다" 비교로는 재시작을 판정할 수 없다.
  const completed =
    status === 'acked' &&
    !!st?.ackedAt &&
    !!server.lastSeenAt &&
    Date.parse(server.lastSeenAt) > Date.parse(st.ackedAt);

  // Current 0-based stage. sent covers both "수신" and "적용 중"; the applying
  // step is highlighted since the agent reports no distinct received signal.
  let stage = 0;
  if (status === 'sent') stage = 2;
  if (status === 'acked') stage = 3;
  if (completed) stage = 4;
  const pct = sent ? [20, 40, 60, 80, 100][stage] : 0;

  const sentAtMs = st?.sentAt ? Date.parse(st.sentAt) : null;
  const applyElapsed = status === 'sent' && sentAtMs ? now - sentAtMs : null;
  const delayed =
    sent && !completed && !failed && startedAt !== null && now - startedAt > SU_DELAY_MS;

  function start() {
    setStartedAt(Date.now());
    act.run(() => api.serverSelfUpdate(tenantId, server.id), () => setSent(true));
  }

  return (
    <Modal title={`에이전트 업데이트 — ${server.name}`} onClose={onClose}>
      <p>
        서버 <b>{server.name}</b>의 에이전트를 최신 코드로 업데이트합니다. 업데이트 중 잠시 오프라인이 될 수 있습니다.
      </p>
      <p className="muted">
        현재 버전 <span className="mono">{server.agentVersion || '알 수 없음'}</span>
      </p>
      {act.error && <p className="err">{act.error}</p>}
      {!sent ? (
        <button className="primary" style={{ marginTop: 14 }} disabled={act.busy} onClick={start}>
          업데이트 실행
        </button>
      ) : (
        <div className="su-progress">
          <div className="su-bar">
            <div className={`su-bar-fill ${failed ? 'failed' : ''}`} style={{ width: `${failed ? 100 : pct}%` }} />
          </div>
          <ol className="su-steps">
            {SU_STAGES.map((label, i) => {
              const cls = completed || i < stage ? 'done' : i === stage && !failed ? 'active' : '';
              return (
                <li key={i} className={`su-step ${cls}`}>
                  <span className="su-dot" aria-hidden="true" />
                  <span>
                    {label}
                    {i === 2 && applyElapsed !== null && (
                      <span className="muted mono"> · {fmtElapsed(applyElapsed)} 경과</span>
                    )}
                  </span>
                </li>
              );
            })}
          </ol>
          {failed && (
            <p className="err" style={{ marginTop: 10 }}>
              업데이트 실패{st?.detail ? ` — ${st.detail}` : ''}. 알림 패널을 확인하세요.
            </p>
          )}
          {completed && (
            <p style={{ marginTop: 10 }}>
              업데이트 완료 — 현재 버전 <span className="mono">{server.agentVersion || '알 수 없음'}</span>
            </p>
          )}
          {delayed && (
            <p className="su-warn" style={{ marginTop: 10 }}>
              지연 — 10분이 지나도 완료되지 않았습니다. 알림 패널을 확인하세요.
            </p>
          )}
        </div>
      )}
    </Modal>
  );
}
