import type { ReactNode } from 'react';
import './globals.css';

export const metadata = {
  title: 'AMX Console',
  description: 'AMX account-switching management console',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
