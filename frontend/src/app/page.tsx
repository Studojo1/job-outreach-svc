'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { Spinner } from '@/components/ui/Spinner';

/**
 * LKOT has been integrated into the main /outreach flow.
 * LinkedIn outreach is now available from the enrichment payment page.
 */
export default function LkotRedirectPage() {
  const router = useRouter();

  useEffect(() => {
    try { localStorage.removeItem('li_wizard_v1'); } catch { }
    router.replace('/onboarding/upload');
  }, [router]);

  return (
    <div className="min-h-screen flex items-center justify-center bg-white">
      <Spinner />
    </div>
  );
}
