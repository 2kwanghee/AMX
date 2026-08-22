'use client';

import { useState } from 'react';
import { api } from '@/lib/api-client/client';
import type { OauthStartResponse, Provider } from '@/lib/api-client/types';
import { CopyButton, Modal, OwnerDatalist, fmtTime, krLabel, useAction } from '../common';

// 계정 등록 — 프로바이더에 따라 경로가 갈린다.
//  claude: §5.5 중앙 OAuth 대행. 인증 코드는 BFF를 통해 정확히 한 번
//    (:oauth-complete) 전달되고, 브라우저는 flowId+code만 잠깐 들고 있는다.
//  codex: OAuth 대행 대상이 아니다(:oauth-start는 claude 전용). 운영자가 로컬에서
//    codex login으로 만든 auth.json을 그대로 반입한다.
export function RegisterModal({
  tenantId,
  ownerSuggestions,
  onClose,
  onDone,
}: {
  tenantId: string;
  ownerSuggestions?: string[];
  onClose: () => void;
  onDone: () => void;
}) {
  const [provider, setProvider] = useState<Provider>('claude');
  const [step, setStep] = useState<1 | 2>(1);
  const [label, setLabel] = useState('');
  const [flow, setFlow] = useState<OauthStartResponse | null>(null);
  const [code, setCode] = useState('');
  const [email, setEmail] = useState('');
  const [assignmentExcluded, setAssignmentExcluded] = useState(false);
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
        <CodexImportFields tenantId={tenantId} ownerSuggestions={ownerSuggestions} onDone={onDone} />
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
          <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <input
              type="checkbox"
              checked={assignmentExcluded}
              onChange={(e) => setAssignmentExcluded(e.target.checked)}
            />
            개인이 자기 프로필에서 직접 쓰는 계정이라 새 배정 대상에서 뺀다
          </label>
          {act.error && <p className="err">{act.error}</p>}
          <button
            className="primary"
            style={{ marginTop: 14 }}
            disabled={act.busy || !code}
            onClick={() =>
              act.run(
                () =>
                  api.oauthComplete(tenantId, {
                    flowId: flow.flowId,
                    code,
                    email: email || undefined,
                    assignmentExcluded: assignmentExcluded || undefined,
                  }),
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
const OWNER_LIST_ID = 'account-owner-suggestions';

function CodexImportFields({
  tenantId,
  ownerSuggestions,
  onDone,
}: {
  tenantId: string;
  ownerSuggestions?: string[];
  onDone: () => void;
}) {
  const [email, setEmail] = useState('');
  const [secret, setSecret] = useState('');
  const [owner, setOwner] = useState('');
  const [price, setPrice] = useState('');
  const [currency, setCurrency] = useState('');
  const [assignmentExcluded, setAssignmentExcluded] = useState(false);
  const act = useAction();
  const priceValid = price.trim() === '' || /^\d+(\.\d+)?$/.test(price.trim());
  const currencyValid = currency.trim() === '' || /^[A-Za-z]{3}$/.test(currency.trim());

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
        list={OWNER_LIST_ID}
      />
      <OwnerDatalist id={OWNER_LIST_ID} options={ownerSuggestions ?? []} />
      <label>월 구독료 (선택)</label>
      <input
        value={price}
        onChange={(e) => setPrice(e.target.value)}
        inputMode="decimal"
        autoComplete="off"
        placeholder="예: 110.00"
      />
      {!priceValid && <p className="err">금액은 0 이상의 숫자만 입력하세요.</p>}
      <label>통화 (선택 — 기본 USD)</label>
      <input
        value={currency}
        onChange={(e) => setCurrency(e.target.value)}
        autoComplete="off"
        maxLength={3}
        placeholder="USD"
      />
      {!currencyValid && <p className="err">통화는 세 글자 코드입니다(예: USD, KRW).</p>}
      <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input
          type="checkbox"
          checked={assignmentExcluded}
          onChange={(e) => setAssignmentExcluded(e.target.checked)}
        />
        개인이 자기 프로필에서 직접 쓰는 계정이라 새 배정 대상에서 뺀다
      </label>
      {act.error && <p className="err">{act.error}</p>}
      <button
        className="primary"
        style={{ marginTop: 14 }}
        disabled={act.busy || !email.trim() || !secret.trim() || !priceValid || !currencyValid}
        onClick={() =>
          act.run(
            () =>
              api.createAccount(tenantId, {
                email: email.trim(),
                provider: 'codex',
                credentialType: 'oauth',
                secret,
                owner: owner.trim() || undefined,
                monthlyPrice: price.trim() === '' ? undefined : price.trim(),
                currency: currency.trim() === '' ? undefined : currency.trim().toUpperCase(),
                assignmentExcluded: assignmentExcluded || undefined,
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
