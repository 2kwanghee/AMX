'use client';

import { useState } from 'react';
import { MeshBackdrop } from '@/components/common';

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
        position: 'relative',
        // 반투명 글로우 — 전역 배경 레이어(도트 그리드·글로우)와 메시가 비쳐
        // 보이도록 불투명 채움 대신 rgba 라디얼을 겹친다.
        background:
          'radial-gradient(1100px 620px at 50% -12%, rgba(15,118,110,0.12), transparent 68%),' +
          ' radial-gradient(900px 520px at 88% 108%, rgba(79,70,229,0.08), transparent 66%)',
      }}
    >
      <MeshBackdrop variant="login" />
      <div className="panel" style={{ width: 380, maxWidth: '100%', marginBottom: 0, position: 'relative' }}>
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
