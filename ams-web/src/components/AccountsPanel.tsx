'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import type { AccountPage, OauthStartResponse, Provider } from '@/lib/api-client/types';
import { Badge, CopyButton, EmailChip, LiveDot, Modal, TimeCell, fmtTime, krLabel, useAction, useMarkOnData } from './common';

const POLL = 8000;

export function AccountsPanel({ tenantId }: { tenantId: string }) {
  const { data, mutate } = useSWR<AccountPage>(
    ['accounts', tenantId],
    () => api.listAccounts(tenantId),
    { refreshInterval: POLL },
  );
  const [wizard, setWizard] = useState(false);
  const [direct, setDirect] = useState(false);
  const act = useAction();
  useMarkOnData(data);
  const accounts = data?.items ?? [];

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>계정<LiveDot /></h2>
        <div className="actions">
          <button className="primary" onClick={() => setWizard(true)}>OAuth 계정 등록</button>
          <button onClick={() => setDirect(true)}>API 키 가져오기</button>
        </div>
      </div>
      {act.error && <p className="err">{act.error}</p>}
      <div className="table-wrap">
        <table>
          <thead><tr><th>이메일</th><th>프로바이더</th><th>유형</th><th>상태</th><th>시크릿</th><th>만료</th><th></th></tr></thead>
          <tbody>
            {accounts.map((a) => (
              <tr key={a.id}>
                <td><EmailChip email={a.email} sub={a.organizationName} /></td>
                <td><Badge value={a.provider} /></td>
                <td>{krLabel(a.credentialType)}</td>
                <td><Badge value={a.status} /></td>
                <td><code>{a.secretMasked}</code></td>
                <td><TimeCell iso={a.credentialExpiresAt} /></td>
                <td>
                  <button
                    className="danger"
                    disabled={act.busy}
                    onClick={() => act.run(() => api.deleteAccount(tenantId, a.id), () => mutate())}
                  >
                    삭제
                  </button>
                </td>
              </tr>
            ))}
            {accounts.length === 0 && (
              <tr><td colSpan={7} className="muted">등록된 계정이 없습니다. 'OAuth 계정 등록'으로 Claude 계정을 연결하세요.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      {wizard && <OauthWizard tenantId={tenantId} onClose={() => setWizard(false)} onDone={() => { setWizard(false); mutate(); }} />}
      {direct && <DirectImport tenantId={tenantId} onClose={() => setDirect(false)} onDone={() => { setDirect(false); mutate(); }} />}
    </div>
  );
}

// §5.5 central OAuth enrollment. The authorization code is submitted through the
// BFF exactly once (:oauth-complete); the browser only holds the flowId + code
// transiently and never touches ams-server directly.
function OauthWizard({
  tenantId,
  onClose,
  onDone,
}: {
  tenantId: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const [step, setStep] = useState<1 | 2>(1);
  const [label, setLabel] = useState('');
  const [flow, setFlow] = useState<OauthStartResponse | null>(null);
  const [code, setCode] = useState('');
  const [email, setEmail] = useState('');
  const act = useAction();
  // Only "claude" is selectable today; the driver for other providers lands in
  // a later step. Kept as a variable so the copy below derives from it.
  const provider: Provider = 'claude';

  return (
    <Modal title="OAuth 계정 등록" onClose={onClose}>
      {step === 1 && (
        <>
          <p className="muted">{krLabel(provider)} 계정을 연결합니다. 라벨(선택)을 입력하고 시작을 누르세요.</p>
          <label>프로바이더</label>
          <select value={provider} disabled>
            <option value="claude">{krLabel('claude')}</option>
            <option value="codex" disabled>Codex — 준비 중</option>
          </select>
          <label>라벨 (선택)</label>
          <input value={label} onChange={(e) => setLabel(e.target.value)} />
          {act.error && <p className="err">{act.error}</p>}
          <button
            className="primary"
            style={{ marginTop: 14 }}
            disabled={act.busy}
            onClick={() =>
              act.run(async () => {
                const f = await api.oauthStart(tenantId, { label: label || undefined, provider });
                setFlow(f);
                setStep(2);
              })
            }
          >
            시작
          </button>
        </>
      )}
      {step === 2 && flow && (
        <>
          <p className="muted">아래 링크에서 로그인·승인 후, 표시되는 코드를 붙여넣으세요.</p>
          <p style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <a href={flow.authorizeUrl} target="_blank" rel="noreferrer">인증 페이지 열기 ↗</a>
            <CopyButton text={flow.authorizeUrl} label="링크 복사" />
          </p>
          <p className="muted">인증 절차 만료 {fmtTime(flow.expiresAt)}</p>
          <label>인증 코드</label>
          <input value={code} onChange={(e) => setCode(e.target.value)} />
          <label>이메일 수동 지정 (선택)</label>
          <input value={email} onChange={(e) => setEmail(e.target.value)} />
          {act.error && <p className="err">{act.error}</p>}
          <button
            className="primary"
            style={{ marginTop: 14 }}
            disabled={act.busy || !code}
            onClick={() =>
              act.run(
                () => api.oauthComplete(tenantId, { flowId: flow.flowId, code, email: email || undefined }),
                onDone,
              )
            }
          >
            등록 완료
          </button>
        </>
      )}
    </Modal>
  );
}

function DirectImport({
  tenantId,
  onClose,
  onDone,
}: {
  tenantId: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const [email, setEmail] = useState('');
  const [secret, setSecret] = useState('');
  const act = useAction();
  return (
    <Modal title="API 키 가져오기" onClose={onClose}>
      <label>이메일</label>
      <input value={email} onChange={(e) => setEmail(e.target.value)} />
      <label>시크릿 (API 키)</label>
      <textarea value={secret} onChange={(e) => setSecret(e.target.value)} rows={3} />
      {act.error && <p className="err">{act.error}</p>}
      <button
        className="primary"
        style={{ marginTop: 14 }}
        disabled={act.busy || !email || !secret}
        onClick={() =>
          act.run(() => api.createAccount(tenantId, { email, credentialType: 'api_key', secret }), onDone)
        }
      >
        가져오기
      </button>
    </Modal>
  );
}
