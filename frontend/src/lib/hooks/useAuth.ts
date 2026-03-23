'use client';

import { useEffect, useState } from 'react';
import api, { ensureAuthToken } from '@/lib/api';
import { useAppStore } from '@/store/useAppStore';

export function useAuth(requireAuth = true) {
  const { user, setUser, orderId, setOrderId, setCandidateId, setCampaignId, setEmailAccountId } = useAppStore();
  const [loading, setLoading] = useState(!user);

  useEffect(() => {
    if (user) {
      setLoading(false);
      return;
    }

    // First exchange BetterAuth session cookie for a JWT (if not already cached),
    // then call the outreach backend /auth/me through the control-plane.
    ensureAuthToken()
      .then(() => api.get('/auth/me'))
      .then(async (res) => {
        const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
        setUser({ ...res.data, timezone: res.data.timezone || browserTz });

        // Auto-recover active order if Zustand lost it
        if (!orderId) {
          try {
            const orderRes = await api.get('/orders/active');
            const order = orderRes.data?.order;
            if (order) {
              setOrderId(order.id);
              if (order.candidate_id) setCandidateId(order.candidate_id);
              if (order.campaign_id) setCampaignId(order.campaign_id);
              if (order.email_account_id) setEmailAccountId(order.email_account_id);
            }
          } catch {
            // No active order — that's fine
          }
        }
      })
      .catch(() => {
        // Auth failed — user stays null
      })
      .finally(() => {
        setLoading(false);
      });
  }, [user, setUser]);

  // Only redirect after loading completes with no authenticated user
  useEffect(() => {
    if (!loading && !user && requireAuth && typeof window !== 'undefined') {
      const platform = process.env.NEXT_PUBLIC_PLATFORM_URL || '';
      window.location.href = `${platform}/auth?mode=signin&redirect=/outreach`;
    }
  }, [loading, user, requireAuth]);

  return { user, loading };
}
