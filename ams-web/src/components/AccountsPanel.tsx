'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { api } from '@/lib/api-client/client';
import type { AccountPage, OauthStartResponse } from '@/lib/api-client/types';
import { Badge, Modal, fmtTime, useAction } from './common';

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
  const accounts = data?.items ?? [];

  return (
    <div className="panel">
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <h2>Accounts</h2>
        <div className="actions">
          <button className="primary" onClick={() => setWizard(true)}>+ OAuth enroll</button>
          <button onClick={() => setDirect(true)}>+ Import (api_key)</button>
        </div>
      </div>
      {act.error && <p className="err">{act.error}</p>}
      <table>
        <thead><tr><th>Email</th><th>Type</th><th>Status</th><th>Secret</th><th>Expires</th><th></th></tr></thead>
        <tbody>
          {accounts.map((a) => (
            <tr key={a.id}>
              <td>{a.email}<div className="muted">{a.organizationName}</div></td>
              <td>{a.credentialType}</td>
              <td><Badge value={a.status} /></td>
              <td><code>{a.secretMasked}</code></td>
              <td className="muted">{fmtTime(a.credentialExpiresAt)}</td>
              <td>
                <button
                  className="danger"
                  disabled={act.busy}
                  onClick={() => act.run(() => api.deleteAccount(tenantId, a.id), () => mutate())}
                >
                  Delete
                </button>
              </td>
            </tr>
          ))}
          {accounts.length === 0 && <tr><td colSpan={6} className="muted">No accounts.</td></tr>}
        </tbody>
      </table>
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

  return (
    <Modal title="OAuth account enrollment" onClose={onClose}>
      {step === 1 && (
        <>
          <p className="muted">Step 1 — start the flow, then open the authorize URL.</p>
          <label>Label (optional)</label>
          <input value={label} onChange={(e) => setLabel(e.target.value)} />
          {act.error && <p className="err">{act.error}</p>}
          <button
            className="primary"
            style={{ marginTop: 14 }}
            disabled={act.busy}
            onClick={() =>
              act.run(async () => {
                const f = await api.oauthStart(tenantId, { label: label || undefined });
                setFlow(f);
                setStep(2);
              })
            }
          >
            Start
          </button>
        </>
      )}
      {step === 2 && flow && (
        <>
          <p className="muted">Step 2 — open the URL, sign in, paste the returned code.</p>
          <p><a href={flow.authorizeUrl} target="_blank" rel="noreferrer">Open authorize URL ↗</a></p>
          <p className="muted">Flow expires {fmtTime(flow.expiresAt)}</p>
          <label>Authorization code</label>
          <input value={code} onChange={(e) => setCode(e.target.value)} />
          <label>Override email (optional)</label>
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
            Complete enrollment
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
    <Modal title="Import api_key account" onClose={onClose}>
      <label>Email</label>
      <input value={email} onChange={(e) => setEmail(e.target.value)} />
      <label>Secret (api key)</label>
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
        Import
      </button>
    </Modal>
  );
}
