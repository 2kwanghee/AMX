'use client';

import { useState } from 'react';

export default function LoginPage() {
  const [password, setPassword] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr('');
    setBusy(true);
    try {
      const res = await fetch('/bff/session', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ password }),
        credentials: 'same-origin',
      });
      if (res.ok) {
        window.location.href = '/dashboard';
        return;
      }
      setErr(res.status === 401 ? 'Invalid credentials.' : 'Login failed.');
    } catch {
      setErr('Network error.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="container" style={{ maxWidth: 380, marginTop: '12vh' }}>
      <div className="panel">
        <h1>AMX Console</h1>
        <p className="muted">Sign in with the operator password.</p>
        <form onSubmit={submit}>
          <label htmlFor="pw">Password</label>
          <input
            id="pw"
            type="password"
            autoFocus
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          {err && <p className="err">{err}</p>}
          <button className="primary" style={{ marginTop: 14, width: '100%' }} disabled={busy}>
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  );
}
