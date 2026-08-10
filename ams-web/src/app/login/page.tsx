'use client';

import { useState } from 'react';

export default function LoginPage() {
  const [email, setEmail] = useState('');
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
        body: JSON.stringify({ email, password }),
        credentials: 'same-origin',
      });
      if (res.ok) {
        window.location.href = '/dashboard';
        return;
      }
      setErr(res.status === 401 ? '이메일 또는 비밀번호가 올바르지 않습니다.' : '로그인에 실패했습니다.');
    } catch {
      setErr('네트워크 오류가 발생했습니다.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 16,
        background: 'linear-gradient(180deg, var(--accent-soft) 0%, var(--bg) 260px)',
      }}
    >
      <div className="panel" style={{ width: 380, maxWidth: '100%', marginBottom: 0 }}>
        <div className="brand" style={{ padding: 0, marginBottom: 4 }}>
          <span className="brand-dot" />
          AMX 관제 콘솔
        </div>
        <p className="muted">관리자 이메일과 비밀번호로 로그인하세요.</p>
        <form onSubmit={submit}>
          <label htmlFor="email">이메일</label>
          <input
            id="email"
            type="email"
            autoComplete="username"
            autoFocus
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
          <label htmlFor="pw">비밀번호</label>
          <input
            id="pw"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          {err && <p className="err">{err}</p>}
          <button className="primary" style={{ marginTop: 16, width: '100%' }} disabled={busy}>
            {busy ? '로그인 중…' : '로그인'}
          </button>
        </form>
      </div>
    </div>
  );
}
