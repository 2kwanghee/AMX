import type { ReactNode } from 'react';
import './globals.css';

export const metadata = {
  title: 'AMX 관제 콘솔',
  description: 'AMX 계정 전환 관리 콘솔',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="ko">
      <body>{children}</body>
    </html>
  );
}
