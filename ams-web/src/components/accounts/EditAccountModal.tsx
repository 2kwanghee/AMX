'use client';

import { useState } from 'react';
import { api } from '@/lib/api-client/client';
import type { Account } from '@/lib/api-client/types';
import { Modal, ProviderTag, useAction } from '../common';

// 계정 수정 — 소유자·이메일. Codex는 이메일을 바꿀 때 서버가 같은 요청 안의
// auth.json으로 신원을 다시 확인하므로(account.codex_email_requires_credential),
// 자격증명 칸을 함께 노출한다. 보내지 않은 필드는 서버가 건드리지 않는다.
export function EditAccount({
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
  const [price, setPrice] = useState(account.monthlyPrice ?? '');
  const [currency, setCurrency] = useState(account.currency ?? 'USD');
  const [assignmentExcluded, setAssignmentExcluded] = useState(account.assignmentExcluded ?? false);
  const act = useAction();
  const isCodex = account.provider === 'codex';
  const emailChanged = email.trim() !== account.email;
  const ownerChanged = owner.trim() !== (account.owner ?? '');
  const priceChanged = price.trim() !== (account.monthlyPrice ?? '');
  const currencyChanged = currency.trim().toUpperCase() !== (account.currency ?? 'USD');
  const assignmentExcludedChanged = assignmentExcluded !== (account.assignmentExcluded ?? false);
  // 금액은 문자열 그대로 전송하므로 Number로 파싱하지 않고 형태만 본다: 음수·비숫자 차단.
  const priceValid = price.trim() === '' || /^\d+(\.\d+)?$/.test(price.trim());
  const currencyValid = /^[A-Za-z]{3}$/.test(currency.trim());
  const dirty =
    emailChanged ||
    ownerChanged ||
    priceChanged ||
    currencyChanged ||
    assignmentExcludedChanged ||
    secret.trim() !== '';

  return (
    <Modal title="계정 수정" onClose={onClose}>
      <p className="muted">
        <b>{account.email}</b> · <ProviderTag value={account.provider} />
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
      <label>월 구독료 (비우면 미설정)</label>
      <input
        value={price}
        onChange={(e) => setPrice(e.target.value)}
        inputMode="decimal"
        autoComplete="off"
        placeholder="예: 110.00"
      />
      {!priceValid && <p className="err">금액은 0 이상의 숫자만 입력하세요.</p>}
      <label>통화</label>
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
      <p className="muted">
        이미 연결된 배정이 있어도 켜는 순간 회수되지는 않습니다. 막히는 건 이 뒤에 새로 생기는 연결뿐입니다.
      </p>
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
        disabled={act.busy || !dirty || !email.trim() || !priceValid || !currencyValid}
        onClick={() =>
          act.run(
            () =>
              api.updateAccount(tenantId, account.id, {
                owner: ownerChanged ? owner.trim() : undefined,
                email: emailChanged ? email.trim() : undefined,
                secret: secret.trim() ? secret : undefined,
                monthlyPrice: priceChanged ? (price.trim() === '' ? null : price.trim()) : undefined,
                currency: currencyChanged ? currency.trim().toUpperCase() : undefined,
                assignmentExcluded: assignmentExcludedChanged ? assignmentExcluded : undefined,
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
