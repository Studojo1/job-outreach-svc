'use client';

import { Suspense, useEffect } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { Button } from '@/components/ui/Button';
import { Container } from '@/components/layout/Container';
import { Spinner } from '@/components/ui/Spinner';
import { useAppStore } from '@/store/useAppStore';
import api from '@/lib/api';

const AUTH_BASE = process.env.NEXT_PUBLIC_AUTH_URL || '/api/v1';

function LoginContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { setUser } = useAppStore();

  useEffect(() => {
    const token = searchParams.get('token');
    if (token) {
      localStorage.setItem('token', token);
      api.get('/auth/me')
        .then((res) => {
          setUser(res.data);
          router.push('/onboarding/upload');
        })
        .catch(() => {
          localStorage.removeItem('token');
        });
    } else {
      const existing = localStorage.getItem('token');
      if (existing) {
        api.get('/auth/me')
          .then((res) => {
            setUser(res.data);
            router.push('/onboarding/upload');
          })
          .catch(() => localStorage.removeItem('token'));
      }
    }
  }, [searchParams, router, setUser]);

  const handleGoogleLogin = () => {
    window.location.href = `${AUTH_BASE}/auth/google/login`;
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-white">
      <Container className="max-w-md">
        <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal text-center p-8">
          <div className="w-16 h-16 rounded-full bg-brand-purple-bg border-2 border-ink flex items-center justify-center mx-auto mb-6">
            <svg className="w-8 h-8 text-primary" viewBox="0 0 24 24" fill="currentColor">
              <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" stroke="currentColor" strokeWidth="2" fill="none" />
            </svg>
          </div>
          <h1 className="font-clash text-2xl font-bold mb-2">Welcome to InternReach</h1>
          <p className="text-sm text-muted font-satoshi mb-8">
            Sign in with Google to get started
          </p>
          <Button onClick={handleGoogleLogin} className="w-full flex items-center justify-center gap-4">
            <svg className="w-5 h-5" viewBox="0 0 24 24">
              <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 01-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" fill="#4285F4"/>
              <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/>
              <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/>
              <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/>
            </svg>
            Sign in with Google
          </Button>
        </div>
      </Container>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense fallback={
      <div className="min-h-screen flex items-center justify-center bg-white">
        <Spinner />
      </div>
    }>
      <LoginContent />
    </Suspense>
  );
}