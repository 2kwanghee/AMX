'use client';

import type { ReactNode } from 'react';
import { useState } from 'react';

// 상태·유형 값의 한글 표시 매핑. className은 원래 영문 값을 유지하고 표시만
// 한글로 바꾼다. 매핑에 없는 값은 원문 그대로 반환한다.
const KR_LABEL: Record<string, string> = {
  online: '온라인',
  offline: '오프라인',
  degraded: '성능 저하',
  quarantined: '격리됨',
  active: '활성',
  available: '사용 가능',
  pending: '대기',
  delivering: '전달 중',
  recalling: '회수 중',
  detached: '해제됨',
  inactive: '비활성',
  disabled: '비활성',
  critical: '심각',
  warning: '경고',
  open: '미확인',
  acked: '확인됨',
  resolved: '해결됨',
  manual: '수동',
  auto: '자동',
  oauth: 'OAuth',
  api_key: 'API 키',
};

export function krLabel(value?: string): string {
  if (!value) return '—';
  return KR_LABEL[value] ?? value;
}

export function Badge({ value }: { value: string }) {
  return (
    <span className={`badge ${value}`}>
      <span className="dot" />
      {krLabel(value)}
    </span>
  );
}

export function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h2>{title}</h2>
          <button onClick={onClose} aria-label="닫기">✕</button>
        </div>
        {children}
      </div>
    </div>
  );
}

/** 클립보드 복사 버튼. 누르면 1.5초간 "복사됨"을 표시한다. */
export function CopyButton({ text, label = '복사' }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="copy-btn"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        } catch {
          /* 클립보드 접근 불가 시 무시 */
        }
      }}
    >
      {copied ? '복사됨' : label}
    </button>
  );
}

/** Wraps an async action with busy + error state, for buttons/forms. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function run(fn: () => Promise<unknown>, onDone?: () => void) {
    setBusy(true);
    setError('');
    try {
      await fn();
      onDone?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return { busy, error, run, setError };
}

export function fmtTime(iso?: string): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString('ko-KR', { dateStyle: 'short', timeStyle: 'short' });
}
