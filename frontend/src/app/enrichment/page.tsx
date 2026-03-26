'use client';

import { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import Script from 'next/script';
import { useAuth } from '@/lib/hooks/useAuth';
import { useAppStore } from '@/store/useAppStore';
import { useOrder } from '@/lib/hooks/useOrder';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { TierSelector } from '@/components/features/TierSelector';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Badge } from '@/components/ui/Badge';
import { Spinner } from '@/components/ui/Spinner';
import { Mail, CheckCircle, Tag, CreditCard, Coins } from 'lucide-react';
import api from '@/lib/api';

declare global {
  interface Window {
    Razorpay: any;
  }
}

interface TierPricing {
  tier: number;
  label: string;
  amount_cents: number;
  currency: string;
  display_price: string;
}

interface CouponResult {
  valid: boolean;
  coupon_id: number;
  discount_type: string;
  discount_value: number;
  original_amount: number;
  discounted_amount: number;
  currency: string;
  distributor: string | null;
}

export default function EnrichmentPage() {
  const router = useRouter();
  const { loading } = useAuth();
  const { candidateId, selectedTier, setSelectedTier, user, orderId } = useAppStore();
  const { updateOrder } = useOrder();

  // Pricing
  const [pricing, setPricing] = useState<TierPricing[]>([]);
  const [currency, setCurrency] = useState('USD');
  const [credits, setCredits] = useState<{ total_credits: number; used_credits: number; available_credits: number } | null>(null);

  // Coupon
  const [couponCode, setCouponCode] = useState('');
  const [couponResult, setCouponResult] = useState<CouponResult | null>(null);
  const [couponLoading, setCouponLoading] = useState(false);
  const [couponError, setCouponError] = useState('');

  // Payment
  const [paying, setPaying] = useState(false);

  // Enrichment
  const [enriching, setEnriching] = useState(false);
  const [enrichProgress, setEnrichProgress] = useState<{ enriched: number; failed: number; total: number; progress: string }>({ enriched: 0, failed: 0, total: 0, progress: '' });
  const [result, setResult] = useState<{ enriched: number; failed: number } | null>(null);
  const [error, setError] = useState('');

  // Load pricing and credits on mount
  useEffect(() => {
    const loadData = async () => {
      try {
        const [pricingRes, creditsRes] = await Promise.all([
          api.get('/payment/pricing'),
          api.get('/payment/credits'),
        ]);
        setPricing(pricingRes.data.tiers || []);
        setCurrency(pricingRes.data.currency || 'USD');
        setCredits(creditsRes.data);
      } catch {
        // pricing fetch failed — fallback tiers will show
      }
    };
    loadData();
  }, []);

  // Poll enrichment job status
  const pollEnrichmentJob = async (jobId: string, total: number) => {
    const poll = async () => {
      try {
        const res = await api.get(`/enrichment/${jobId}/status`);
        const data = res.data;
        setEnrichProgress({ enriched: data.enriched, failed: data.failed, total, progress: data.progress });

        if (data.status === 'completed') {
          setResult({ enriched: data.enriched, failed: data.failed });
          setEnriching(false);
          updateOrder({ status: 'enrichment_complete', log_entry: `Enriched ${data.enriched} leads` });
          try {
            const creditsRes = await api.get('/payment/credits');
            setCredits(creditsRes.data);
          } catch { /* non-critical */ }
          return;
        }
        if (data.status === 'failed') {
          setError(data.error || 'Enrichment failed');
          setEnriching(false);
          try {
            const creditsRes = await api.get('/payment/credits');
            setCredits(creditsRes.data);
          } catch { /* non-critical */ }
          return;
        }
        // Still processing — poll again
        setTimeout(poll, 3000);
      } catch {
        setError('Lost connection to enrichment job');
        setEnriching(false);
      }
    };
    setTimeout(poll, 2000); // first poll after 2s
  };

  const validateCoupon = async () => {
    if (!couponCode.trim()) return;
    setCouponLoading(true);
    setCouponError('');
    setCouponResult(null);
    try {
      const res = await api.post('/payment/coupon/validate', {
        code: couponCode.trim(),
        tier: selectedTier,
        currency,
      });
      setCouponResult(res.data);
    } catch (err: any) {
      setCouponError(err.response?.data?.detail || 'Invalid coupon');
    } finally {
      setCouponLoading(false);
    }
  };

  const handleEnrich = async (limit: number) => {
    if (!candidateId) return;
    setEnriching(true);
    setEnrichProgress({ enriched: 0, failed: 0, total: limit, progress: 'Starting enrichment...' });
    setError('');
    try {
      const res = await api.post('/enrichment/enrich', { candidate_id: candidateId, limit, order_id: orderId });
      if (res.data.job_id) {
        // Background job started — poll for progress
        pollEnrichmentJob(res.data.job_id, res.data.total || limit);
      } else {
        // Legacy synchronous response
        setResult({ enriched: res.data.enriched, failed: res.data.failed });
        setEnriching(false);
        updateOrder({ status: 'enrichment_complete', log_entry: `Enriched ${res.data.enriched} leads` });
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Enrichment failed');
      setEnriching(false);
    }
  };

  const handlePayAndEnrich = async () => {
    if (!candidateId) return;

    // Check if user already has enough credits
    if (credits && credits.available_credits >= selectedTier) {
      handleEnrich(selectedTier);
      return;
    }

    // Create Razorpay order
    setPaying(true);
    setError('');
    try {
      const orderRes = await api.post('/payment/create-order', {
        tier: selectedTier,
        currency,
        coupon_code: couponResult?.valid ? couponCode.trim() : undefined,
      });

      // If fully discounted (100% coupon)
      if (orderRes.data.free) {
        setCredits((prev) => prev
          ? { ...prev, total_credits: prev.total_credits + orderRes.data.credits_granted, available_credits: prev.available_credits + orderRes.data.credits_granted }
          : { total_credits: orderRes.data.credits_granted, used_credits: 0, available_credits: orderRes.data.credits_granted }
        );
        setPaying(false);
        handleEnrich(selectedTier);
        return;
      }

      const provider = orderRes.data.provider || 'razorpay';
      console.error('[PAYMENT] provider:', provider, 'checkout_url:', orderRes.data.checkout_url);

      // ── External checkout (Dodo / any gateway with checkout_url) ──
      if (orderRes.data.checkout_url) {
        console.error('[PAYMENT] Redirecting to checkout:', orderRes.data.checkout_url);
        localStorage.setItem('dodo_pending_tier', String(selectedTier));
        window.location.href = orderRes.data.checkout_url;
        return;
      }

      console.error('[PAYMENT] Razorpay path, key_id:', orderRes.data.key_id);
      // ── Razorpay (India) — modal checkout ──
      const options = {
        key: orderRes.data.key_id,
        amount: orderRes.data.amount,
        currency: orderRes.data.currency || 'INR',
        name: 'OpportunityApply',
        description: `${selectedTier} Email Enrichment Credits`,
        order_id: orderRes.data.order_id,
        handler: async (response: { razorpay_order_id: string; razorpay_payment_id: string; razorpay_signature: string }) => {
          try {
            await api.post('/payment/verify', {
              razorpay_order_id: response.razorpay_order_id,
              razorpay_payment_id: response.razorpay_payment_id,
              razorpay_signature: response.razorpay_signature,
            });
            // Refresh credits and start enrichment
            const creditsRes = await api.get('/payment/credits');
            setCredits(creditsRes.data);
            setPaying(false);
            handleEnrich(selectedTier);
          } catch (err: any) {
            setError(err.response?.data?.detail || 'Payment verification failed');
            setPaying(false);
          }
        },
        prefill: {
          email: user?.email || '',
          name: user?.name || '',
        },
        theme: { color: '#7C3AED' },
        modal: {
          ondismiss: () => { setPaying(false); },
        },
      };

      const rzp = new window.Razorpay(options);
      rzp.on('payment.failed', (response: any) => {
        setError(response.error?.description || 'Payment failed');
        setPaying(false);
      });
      rzp.open();
    } catch (err: any) {
      console.error('[PAYMENT] Error:', err);
      setError(err.response?.data?.detail || err.message || 'Failed to create payment order');
      setPaying(false);
    }
  };

  if (loading) return <div className="min-h-screen flex items-center justify-center bg-white"><Spinner /></div>;
  if (!candidateId) { router.push('/onboarding/upload'); return null; }

  const selectedPricing = pricing.find((p) => p.tier === selectedTier);
  const displayPrice = couponResult?.valid
      ? `${currency === 'INR' ? '₹' : '$'}${(couponResult.discounted_amount / 100).toFixed(0)}`
      : selectedPricing?.display_price || (selectedTier === 200 ? '$20' : selectedTier === 350 ? '$27' : '$40');

  const hasEnoughCredits = credits ? credits.available_credits >= selectedTier : false;

  return (
    <div className="min-h-screen bg-white">
      <Navbar />
      <Script src="https://checkout.razorpay.com/v1/checkout.js" strategy="lazyOnload" />
      <Container className="max-w-onboarding py-8">
        <div className="text-center mb-10">
          <div className="w-14 h-14 rounded-xl bg-brand-purple-bg border-2 border-ink flex items-center justify-center mx-auto text-primary mb-6">
            <Mail className="w-7 h-7" />
          </div>
          <h1 className="font-clash text-2xl font-bold">Email Enrichment</h1>
          <p className="text-sm text-muted mt-2 font-satoshi">Choose how many leads to enrich with verified email addresses.</p>
        </div>

        {/* Credit Balance */}
        {credits && credits.total_credits > 0 && (
          <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-4 mb-6 flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="w-9 h-9 rounded-xl bg-studojo-green-bg border-2 border-ink flex items-center justify-center">
                <Coins className="w-5 h-5 text-secondary" />
              </div>
              <div>
                <p className="text-sm font-bold font-satoshi">Your Credits</p>
                <p className="text-xs text-muted font-satoshi">{credits.available_credits} available / {credits.total_credits} total</p>
              </div>
            </div>
            <Badge variant="success">{credits.available_credits} credits</Badge>
          </div>
        )}

        {result ? (
          <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-8 text-center animate-fade-in">
            <div className="w-12 h-12 rounded-full bg-studojo-green-bg border-2 border-ink flex items-center justify-center mx-auto mb-6">
              <CheckCircle className="w-6 h-6 text-secondary" />
            </div>
            <h2 className="font-clash text-2xl font-bold mb-2">Enrichment Complete</h2>
            <p className="text-base text-muted font-satoshi">
              <span className="text-secondary font-bold">{result.enriched}</span> emails verified
              {result.failed > 0 && <span className="text-muted"> ({result.failed} not found)</span>}
            </p>
            <Button onClick={() => router.push('/connect/gmail')} className="mt-8">Connect Gmail to Send Emails</Button>
          </div>
        ) : enriching ? (
          <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-8">
            <div className="text-center mb-6">
              <p className="text-base text-ink font-bold font-satoshi">Enriching leads...</p>
              <p className="text-sm text-muted font-satoshi mt-1">{enrichProgress.progress}</p>
            </div>
            <div className="max-w-md mx-auto space-y-6">
              {/* Progress bar */}
              <div>
                <div className="flex justify-between text-xs text-muted font-satoshi mb-2">
                  <span>{enrichProgress.enriched} enriched{enrichProgress.failed > 0 ? `, ${enrichProgress.failed} failed` : ''}</span>
                  <span>{enrichProgress.total} total</span>
                </div>
                <div className="h-3 rounded-full bg-surface-muted border-2 border-ink/20 overflow-hidden">
                  <div
                    className="h-full rounded-full bg-primary transition-all duration-500"
                    style={{ width: `${enrichProgress.total > 0 ? ((enrichProgress.enriched + enrichProgress.failed) / enrichProgress.total) * 100 : 0}%` }}
                  />
                </div>
              </div>
              {/* Live status cards */}
              <div className="grid grid-cols-2 gap-4">
                <div className="p-4 rounded-2xl border-2 border-ink bg-studojo-green-bg text-center">
                  <p className="text-2xl font-bold font-clash text-secondary">{enrichProgress.enriched}</p>
                  <p className="text-xs text-muted font-satoshi mt-1">Emails Found</p>
                </div>
                <div className="p-4 rounded-2xl border-2 border-ink/30 bg-white text-center">
                  <p className="text-2xl font-bold font-clash text-muted">{enrichProgress.failed}</p>
                  <p className="text-xs text-muted font-satoshi mt-1">Not Found</p>
                </div>
              </div>
              <div className="flex items-center justify-center gap-3">
                <Spinner size="sm" />
                <span className="text-sm text-muted font-satoshi">This may take a few minutes for large batches</span>
              </div>
            </div>
          </div>
        ) : (
          <>
            <TierSelector
              selected={selectedTier}
              onSelect={(tier) => { setSelectedTier(tier); setCouponResult(null); setCouponError(''); }}
              pricing={pricing}
            />

            {/* Coupon Section */}
            {!hasEnoughCredits && (
              <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-6 mt-6">
                <div className="flex items-center gap-3 mb-4">
                  <Tag className="w-5 h-5 text-primary" />
                  <h3 className="font-clash text-base font-bold">Have a coupon?</h3>
                </div>
                <div className="flex gap-3">
                  <Input
                    value={couponCode}
                    onChange={(e) => { setCouponCode(e.target.value.toUpperCase()); setCouponResult(null); setCouponError(''); }}
                    placeholder="Enter coupon code"
                    className="flex-1"
                  />
                  <Button variant="outline" onClick={validateCoupon} loading={couponLoading}>Apply</Button>
                </div>
                {couponError && <p className="text-error text-xs mt-2 font-satoshi">{couponError}</p>}
                {couponResult?.valid && (
                  <div className="mt-3 p-3 bg-studojo-green-bg rounded-xl border-2 border-ink/20">
                    <p className="text-sm text-secondary font-bold font-satoshi">
                      {couponResult.discount_type === 'percent'
                        ? `${couponResult.discount_value}% off`
                        : `${currency === 'INR' ? '₹' : '$'}${(couponResult.discount_value / 100).toFixed(0)} off`
                      }
                      {couponResult.distributor && <span className="text-muted font-normal"> via {couponResult.distributor}</span>}
                    </p>
                    <p className="text-xs text-muted font-satoshi mt-1">
                      <span className="line-through">{currency === 'INR' ? '₹' : '$'}{(couponResult.original_amount / 100).toFixed(0)}</span>
                      {' → '}
                      <span className="text-secondary font-bold">{currency === 'INR' ? '₹' : '$'}{(couponResult.discounted_amount / 100).toFixed(0)}</span>
                    </p>
                  </div>
                )}
              </div>
            )}

            {error && <p className="text-error text-sm text-center mt-6 font-satoshi">{error}</p>}

            <div className="flex flex-col items-center gap-3 mt-10">
              {hasEnoughCredits ? (
                <Button size="lg" onClick={() => handleEnrich(selectedTier)}>
                  <Coins className="w-5 h-5 mr-2 inline" /> Enrich {selectedTier} Leads (Use Credits)
                </Button>
              ) : (
                <Button size="lg" onClick={handlePayAndEnrich} loading={paying}>
                  <CreditCard className="w-5 h-5 mr-2 inline" /> Pay {displayPrice} & Enrich {selectedTier} Leads
                </Button>
              )}
            </div>
          </>
        )}
      </Container>
    </div>
  );
}
