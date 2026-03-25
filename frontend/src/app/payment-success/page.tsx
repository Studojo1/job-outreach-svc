'use client';

import { Suspense, useState, useEffect, useCallback } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { Spinner } from '@/components/ui/Spinner';
import { Button } from '@/components/ui/Button';
import { CheckCircle, XCircle, Clock } from 'lucide-react';
import api from '@/lib/api';

function PaymentSuccessContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { loading: authLoading } = useAuth();

  const sessionId = searchParams.get('session_id');

  const [status, setStatus] = useState<'polling' | 'paid' | 'failed' | 'error'>('polling');
  const [error, setError] = useState('');

  const pollPayment = useCallback(async (attempts: number) => {
    if (!sessionId) {
      setStatus('error');
      setError('No session ID found');
      return;
    }

    try {
      const res = await api.post('/payment/verify-dodo', { session_id: sessionId });
      const data = res.data;

      if (data.status === 'paid') {
        setStatus('paid');
        return;
      }
      if (data.status === 'failed') {
        setStatus('failed');
        return;
      }

      // Still pending — retry up to 30 times (90 seconds)
      if (attempts < 30) {
        setTimeout(() => pollPayment(attempts + 1), 3000);
      } else {
        setStatus('error');
        setError('Payment confirmation timed out. If you were charged, your credits will be added automatically.');
      }
    } catch {
      if (attempts < 5) {
        setTimeout(() => pollPayment(attempts + 1), 3000);
      } else {
        setStatus('error');
        setError('Failed to verify payment. Please contact support if you were charged.');
      }
    }
  }, [sessionId]);

  useEffect(() => {
    if (!authLoading && sessionId) {
      pollPayment(0);
    }
  }, [authLoading, sessionId, pollPayment]);

  const handleContinue = () => {
    localStorage.removeItem('dodo_pending_tier');
    router.push('/enrichment');
  };

  if (authLoading) {
    return <div className="min-h-screen flex items-center justify-center bg-white"><Spinner /></div>;
  }

  return (
    <div className="min-h-screen bg-white">
      <Navbar />
      <Container className="max-w-onboarding py-16">
        <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-8 text-center">
          {status === 'polling' && (
            <>
              <div className="w-14 h-14 rounded-full bg-brand-purple-bg border-2 border-ink flex items-center justify-center mx-auto mb-6">
                <Clock className="w-7 h-7 text-primary" />
              </div>
              <h1 className="font-clash text-2xl font-bold mb-2">Confirming Payment</h1>
              <p className="text-sm text-muted font-satoshi mb-6">Please wait while we confirm your payment...</p>
              <Spinner />
            </>
          )}

          {status === 'paid' && (
            <>
              <div className="w-14 h-14 rounded-full bg-studojo-green-bg border-2 border-ink flex items-center justify-center mx-auto mb-6">
                <CheckCircle className="w-7 h-7 text-secondary" />
              </div>
              <h1 className="font-clash text-2xl font-bold mb-2">Payment Successful</h1>
              <p className="text-sm text-muted font-satoshi mb-8">Your credits have been added. Continue to enrich your leads.</p>
              <Button size="lg" onClick={handleContinue}>Continue to Enrichment</Button>
            </>
          )}

          {status === 'failed' && (
            <>
              <div className="w-14 h-14 rounded-full bg-red-50 border-2 border-ink flex items-center justify-center mx-auto mb-6">
                <XCircle className="w-7 h-7 text-red-500" />
              </div>
              <h1 className="font-clash text-2xl font-bold mb-2">Payment Failed</h1>
              <p className="text-sm text-muted font-satoshi mb-8">Your payment could not be processed. Please try again.</p>
              <Button size="lg" variant="outline" onClick={() => router.push('/enrichment')}>Back to Enrichment</Button>
            </>
          )}

          {status === 'error' && (
            <>
              <div className="w-14 h-14 rounded-full bg-yellow-50 border-2 border-ink flex items-center justify-center mx-auto mb-6">
                <Clock className="w-7 h-7 text-yellow-600" />
              </div>
              <h1 className="font-clash text-2xl font-bold mb-2">Verification Pending</h1>
              <p className="text-sm text-muted font-satoshi mb-8">{error}</p>
              <Button size="lg" variant="outline" onClick={() => router.push('/enrichment')}>Back to Enrichment</Button>
            </>
          )}
        </div>
      </Container>
    </div>
  );
}

export default function PaymentSuccessPage() {
  return (
    <Suspense fallback={<div className="min-h-screen flex items-center justify-center bg-white"><Spinner /></div>}>
      <PaymentSuccessContent />
    </Suspense>
  );
}
