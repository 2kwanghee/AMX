'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import type { Account, AccountPage, OauthStartResponse, Provider } from '@/lib/api-client/types';
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
  const [editing, setEditing] = useState<Account | null>(null);
  const act = useAction();
  useMarkOnData(data);
  const accounts = data?.items ?? [];

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>계정<LiveDot /></h2>
        <div className="actions">
          <button className="primary" onClick={() => setWizard(true)}>계정 등록</button>
          <button onClick={() => setDirect(true)}>API 키 가져오기</button>
        </div>
      </div>
      {act.error && <p className="err">{act.error}</p>}
      <div className="table-wrap">
        <table>
          <thead><tr><th>이메일</th><th>프로바이더</th><th>소유자</th><th>유형</th><th>상태</th><th>시크릿</th><th>만료</th><th></th></tr></thead>
          <tbody>
            {accounts.map((a) => (
              <tr key={a.id}>
                <td><EmailChip email={a.email} sub={a.organizationName} /></td>
                <td><Badge value={a.provider} /></td>
                <td>{a.owner ? a.owner : <span className="muted">—</span>}</td>
                <td>{krLabel(a.credentialType)}</td>
                <td><Badge value={a.status} /></td>
                <td><code>{a.secretMasked}</code></td>
                <td><TimeCell iso={a.credentialExpiresAt} /></td>
                <td>
                  <div className="actions row-actions">
                    <button disabled={act.busy} onClick={() => setEditing(a)}>수정</button>
                    <button
                      className="danger"
                      disabled={act.busy}
                      onClick={() => act.run(() => api.deleteAccount(tenantId, a.id), () => mutate())}
                    >
                      삭제
                    </button>
                  </div>
                </td>
              </tr>
            ))}
            {accounts.length === 0 && (
              <tr><td colSpan={8} className="muted">등록된 계정이 없습니다. '계정 등록'으로 Claude·Codex 계정을 연결하세요.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      {wizard && <RegisterModal tenantId={tenantId} onClose={() => setWizard(false)} onDone={() => { setWizard(false); mutate(); }} />}
      {direct && <DirectImport tenantId={tenantId} onClose={() => setDirect(false)} onDone={() => { setDirect(false); mutate(); }} />}
      {editing && (
        <EditAccount
          tenantId={tenantId}
          account={editing}
          onClose={() => setEditing(null)}
          onDone={() => { setEditing(null); mutate(); }}
        />
      )}
    </div>
  );
}

// 계정 등록 — 프로바이더에 따라 경로가 갈린다.
//  claude: §5.5 중앙 OAuth 대행. 인증 코드는 BFF를 통해 정확히 한 번
//    (:oauth-complete) 전달되고, 브라우저는 flowId+code만 잠깐 들고 있는다.
//  codex: OAuth 대행 대상이 아니다(:oauth-start는 claude 전용). 운영자가 로컬에서
//    codex login으로 만든 auth.json을 그대로 반입한다.
function RegisterModal({
  tenantId,
  onClose,
  onDone,
}: {
  tenantId: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const [provider, setProvider] = useState<Provider>('claude');
  const [step, setStep] = useState<1 | 2>(1);
  const [label, setLabel] = useState('');
  const [flow, setFlow] = useState<OauthStartResponse | null>(null);
  const [code, setCode] = useState('');
  const [email, setEmail] = useState('');
  const act = useAction();

  const providerSelect = (
    <>
      <label>프로바이더</label>
      <select
        value={provider}
        onChange={(e) => {
          act.setError('');
          setProvider(e.target.value as Provider);
        }}
      >
        <option value="claude">{krLabel('claude')}</option>
        <option value="codex">{krLabel('codex')}</option>
      </select>
    </>
  );

  if (provider === 'codex') {
    return (
      <Modal title="Codex 계정 반입" onClose={onClose}>
        {providerSelect}
        <CodexImportFields tenantId={tenantId} onDone={onDone} />
      </Modal>
    );
  }

  return (
    <Modal title="OAuth 계정 등록" onClose={onClose}>
      {step === 1 && (
        <>
          <p className="muted">{krLabel(provider)} 계정을 연결합니다. 라벨(선택)을 입력하고 시작을 누르세요.</p>
          {providerSelect}
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

// Codex 반입 폼. secret은 auth.json 전문이라 마스킹하지 않고 그대로 보여준다
// (운영자가 붙여넣은 것을 눈으로 확인해야 한다). 대신 등록에 성공하면 즉시
// 상태에서 지우고, 어떤 경로로도 로깅하지 않는다. 모달을 닫으면 컴포넌트가
// 언마운트되므로 실패한 채 남은 값도 함께 사라진다.
function CodexImportFields({ tenantId, onDone }: { tenantId: string; onDone: () => void }) {
  const [email, setEmail] = useState('');
  const [secret, setSecret] = useState('');
  const [owner, setOwner] = useState('');
  const act = useAction();

  return (
    <>
      <p className="muted" style={{ marginTop: 10 }}>
        Codex는 중앙 OAuth 대행 대상이 아닙니다. 로컬에서 <code>codex login</code>을 마친 뒤
        <code> ~/.codex/auth.json</code> 내용을 그대로 붙여넣으세요.
      </p>
      <label>이메일</label>
      <input
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        autoComplete="off"
        placeholder="auth.json의 id_token과 같은 계정이어야 합니다"
      />
      <label>auth.json 전문</label>
      <textarea
        value={secret}
        onChange={(e) => setSecret(e.target.value)}
        rows={8}
        spellCheck={false}
        autoComplete="off"
        placeholder={'{ "OPENAI_API_KEY": null, "tokens": { … } }'}
      />
      <label>소유자 (선택)</label>
      <input
        value={owner}
        onChange={(e) => setOwner(e.target.value)}
        autoComplete="off"
        placeholder="담당자·팀 이름"
      />
      {act.error && <p className="err">{act.error}</p>}
      <button
        className="primary"
        style={{ marginTop: 14 }}
        disabled={act.busy || !email.trim() || !secret.trim()}
        onClick={() =>
          act.run(
            () =>
              api.createAccount(tenantId, {
                email: email.trim(),
                provider: 'codex',
                credentialType: 'oauth',
                secret,
                owner: owner.trim() || undefined,
              }),
            () => {
              setSecret('');
              onDone();
            },
          )
        }
      >
        반입
      </button>
    </>
  );
}

// 계정 수정 — 소유자·이메일. Codex는 이메일을 바꿀 때 서버가 같은 요청 안의
// auth.json으로 신원을 다시 확인하므로(account.codex_email_requires_credential),
// 자격증명 칸을 함께 노출한다. 보내지 않은 필드는 서버가 건드리지 않는다.
function EditAccount({
  tenantId,
  account,
  onClose,
  onDone,
}: {
  tenantId: string;
  account: Account;
  onClose: () => void;
  onDone: () => void;
}) {
  const [owner, setOwner] = useState(account.owner ?? '');
  const [email, setEmail] = useState(account.email);
  const [secret, setSecret] = useState('');
  const act = useAction();
  const isCodex = account.provider === 'codex';
  const emailChanged = email.trim() !== account.email;
  const ownerChanged = owner.trim() !== (account.owner ?? '');
  const dirty = emailChanged || ownerChanged || secret.trim() !== '';

  return (
    <Modal title="계정 수정" onClose={onClose}>
      <p className="muted">
        <b>{account.email}</b> · {krLabel(account.provider)}
      </p>
      <label>소유자</label>
      <input
        value={owner}
        onChange={(e) => setOwner(e.target.value)}
        autoComplete="off"
        placeholder="담당자·팀 이름 (비우면 지워집니다)"
      />
      <label>이메일</label>
      <input value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="off" />
      {isCodex && (
        <>
          <label>auth.json 전문 {emailChanged ? '(이메일 변경 시 필수)' : '(선택 — 교체할 때만)'}</label>
          <textarea
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
            rows={6}
            spellCheck={false}
            autoComplete="off"
          />
        </>
      )}
      {act.error && <p className="err">{act.error}</p>}
      <button
        className="primary"
        style={{ marginTop: 14 }}
        disabled={act.busy || !dirty || !email.trim()}
        onClick={() =>
          act.run(
            () =>
              api.updateAccount(tenantId, account.id, {
                owner: ownerChanged ? owner.trim() : undefined,
                email: emailChanged ? email.trim() : undefined,
                secret: secret.trim() ? secret : undefined,
              }),
            () => {
              setSecret('');
              onDone();
            },
          )
        }
      >
        저장
      </button>
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
