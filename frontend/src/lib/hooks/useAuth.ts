'use client';

import { useEffect, useState } from 'react';
import api from '@/lib/api';
import { useAppStore } from '@/store/useAppStore';

export function useAuth(requireAuth = true) {
  const { user, setUser } = useAppStore();
  const [loading, setLoading] = useState(!user);

  useEffect(() => {
    if (user) {
      setLoading(false);
      return;
    }

    api.get('/auth/me')
      .then((res) => {
        const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
        setUser({ ...res.data, timezone: res.data.timezone || browserTz });
        setLoading(false);
      })
      .catch(() => {
        setLoading(false);
        // Auth check finished — user is not authenticated.
        // Redirect to Studojo login only after loading is complete.
        if (requireAuth && typeof window !== 'undefined') {
          window.location.href = 'https://studojo.com/auth?mode=signin&redirect=/outreach';
        }
      });
  }, [requireAuth, user, setUser]);

  return { user, loading };
}
