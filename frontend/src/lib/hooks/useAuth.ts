'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import api from '@/lib/api';
import { useAppStore } from '@/store/useAppStore';

export function useAuth(requireAuth = true) {
  const router = useRouter();
  const { user, setUser } = useAppStore();

  useEffect(() => {
    const token = localStorage.getItem('token');
    if (!token && requireAuth) {
      router.push('/login');
      return;
    }
    if (token && !user) {
      api.get('/auth/me')
        .then((res) => setUser(res.data))
        .catch(() => {
          localStorage.removeItem('token');
          if (requireAuth) router.push('/login');
        });
    }
  }, [requireAuth, router, user, setUser]);

  return { user };
}
