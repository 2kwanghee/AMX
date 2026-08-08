import { redirect } from 'next/navigation';

// Middleware already gates auth; an authenticated visitor lands on the console.
export default function Home() {
  redirect('/dashboard');
}
