'use client';

import { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import Script from 'next/script';
import { useAuth } from '@/lib/hooks/useAuth';
import { useAppStore } from '@/store/useAppStore';
import { useOrder } from '@/lib/hooks/useOrder';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Badge } from '@/components/ui/Badge';
import { Spinner } from '@/components/ui/Spinner';
import { Mail, CheckCircle, Tag, CreditCard, Coins, Linkedin, Zap } from 'lucide-react';
import api from '@/lib/api';
import type { PlanType } from '@/store/useAppStore';

declare global {
  interface Window { Razorpay: any; }
}

interface PlanPricing {
  plan_id: string;
  plan_type: string;
  label: string;
  email_credits: number;
  linkedin_credits: number;
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

// Original prices for both-plan savings calculation
const BOTH_SAVINGS: Record<string, { savePct: number }> = {
  both_200: { savePct: 12 },
  both_350: { savePct: 17 },
  both_500: { savePct: 30 },
};

type ChannelTab = 'email' | 'linkedin' | 'both';

export default function EnrichmentPage() {
  const router = useRouter();
  const { loading } = useAuth();
  const { candidateId, selectedTier, setSelectedTier, user, orderId, setPlanType } = useAppStore();
  const { updateOrder } = useOrder();

  // Pricing
  const [allPlans, setAllPlans] = useState<PlanPricing[]>([]);
  const [currency, setCurrency] = useState('USD');
  const [credits, setCredits] = useState<{ total_credits: number; used_credits: number; available_credits: number } | null>(null);

  // Channel selection
  const [channelTab, setChannelTab] = useState<ChannelTab>('email');
  const [selectedPlanId, setSelectedPlanId] = useState<string>('email_350');

  // Coupon
  const [couponCode, setCouponCode] = useState('');
  const [couponResult, setCouponResult] = useState<CouponResult | null>(null);
  const [couponLoading, setCouponLoading] = useState(false);
  const [couponError, setCouponError] = useState('');

  // Payment
  const [paying, setPaying] = useState(false);
  const [redirectUrl, setRedirectUrl] = useState('');

  // Enrichment (email path)
  const [enriching, setEnriching] = useState(false);
  const [enrichProgress, setEnrichProgress] = useState<{ enriched: number; failed: number; total: number; progress: string }>({ enriched: 0, failed: 0, total: 0, progress: '' });
  const [result, setResult] = useState<{ enriched: number; failed: number; planType: PlanType } | null>(null);
  const [error, setError] = useState('');

  // Load pricing + credits on mount; auto-advance if enrichment already done
  useEffect(() => {
    const loadData = async () => {
      try {
        const [pricingRes, creditsRes, orderRes] = await Promise.all([
          api.get('/payment/pricing'),
          api.get('/payment/credits'),
          api.get('/orders/active'),
        ]);
        setAllPlans(pricingRes.data.plans || []);
        setCurrency(pricingRes.data.currency || 'USD');
        setCredits(creditsRes.data);

        // If enrichment already finished (e.g. polling dropped), skip ahead
        const order = orderRes.data?.order;
        if (order && ['enrichment_complete', 'campaign_setup', 'email_connected', 'campaign_running', 'completed'].includes(order.status)) {
          const pt = order.plan_type || 'email';
          setPlanType(pt as PlanType);
          router.push(pt === 'linkedin' ? '/connect/linkedin' : '/connect/gmail');
        }
      } catch { /* fallback prices will show */ }
    };
    loadData();
  }, []);

  // Plans filtered by active channel tab
  const visiblePlans = allPlans.filter((p) => p.plan_type === channelTab);

  const activePlan = allPlans.find((p) => p.plan_id === selectedPlanId);

  // When channel tab changes, auto-select the Growth tier for that channel
  const switchChannel = (tab: ChannelTab) => {
    setChannelTab(tab);
    const growth = allPlans.find((p) => p.plan_type === tab && p.label === 'Growth');
    if (growth) {
      setSelectedPlanId(growth.plan_id);
      // Mirror volume into selectedTier for legacy enrichment code
      const vol = growth.email_credits || growth.linkedin_credits;
      setSelectedTier(vol as 200 | 350 | 500);
    }
    setCouponResult(null);
    setCouponError('');
  };

  const selectPlan = (plan: PlanPricing) => {
    setSelectedPlanId(plan.plan_id);
    const vol = plan.email_credits || plan.linkedin_credits;
    setSelectedTier(vol as 200 | 350 | 500);
    setCouponResult(null);
    setCouponError('');
  };

  const pollEnrichmentJob = async (jobId: string, total: number) => {
    const poll = async () => {
      try {
        const res = await api.get(`/enrichment/${jobId}/status`);
        const data = res.data;
        setEnrichProgress({ enriched: data.enriched, failed: data.failed, total, progress: data.progress });
        if (data.status === 'completed') {
          setResult({ enriched: data.enriched, failed: data.failed, planType: channelTab });
          setEnriching(false);
          updateOrder({ status: 'enrichment_complete', log_entry: `Enriched ${data.enriched} leads` });
          try { const cr = await api.get('/payment/credits'); setCredits(cr.data); } catch { }
          return;
        }
        if (data.status === 'failed') {
          setError(data.error || 'Enrichment failed');
          setEnriching(false);
          return;
        }
        setTimeout(poll, 3000);
      } catch { setError('Lost connection to enrichment job'); setEnriching(false); }
    };
    setTimeout(poll, 2000);
  };

  const validateCoupon = async () => {
    if (!couponCode.trim()) return;
    setCouponLoading(true);
    setCouponError('');
    setCouponResult(null);
    try {
      const res = await api.post('/payment/coupon/validate', {
        code: couponCode.trim(),
        plan_id: selectedPlanId,
        currency,
      });
      setCouponResult(res.data);
    } catch (err: any) {
      setCouponError(err.response?.data?.detail || 'Invalid coupon');
    } finally { setCouponLoading(false); }
  };

  const handleEnrich = async (limit: number) => {
    if (!candidateId) return;
    setEnriching(true);
    setEnrichProgress({ enriched: 0, failed: 0, total: limit, progress: 'Starting enrichment...' });
    setError('');
    try {
      const res = await api.post('/enrichment/enrich', { candidate_id: candidateId, limit, order_id: orderId });
      if (res.data.job_id) {
        pollEnrichmentJob(res.data.job_id, res.data.total || limit);
      } else {
        setResult({ enriched: res.data.enriched, failed: res.data.failed, planType: channelTab });
        setEnriching(false);
        updateOrder({ status: 'enrichment_complete', log_entry: `Enriched ${res.data.enriched} leads` });
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Enrichment failed');
      setEnriching(false);
    }
  };

  const handlePayAndProceed = async () => {
    if (!candidateId || !activePlan) return;

    const planType = activePlan.plan_type as PlanType;

    // Email-only with enough credits — skip payment
    if (planType === 'email' && credits && credits.available_credits >= activePlan.email_credits) {
      setPlanType('email');
      handleEnrich(activePlan.email_credits);
      return;
    }

    setPaying(true);
    setError('');
    try {
      const orderRes = await api.post('/payment/create-order', {
        plan_id: selectedPlanId,
        currency,
        coupon_code: couponResult?.valid ? couponCode.trim() : undefined,
      });

      // Save plan type to store immediately so connect pages know where to route
      setPlanType(planType);

      if (orderRes.data.free) {
        setCredits((prev) => prev
          ? { ...prev, total_credits: prev.total_credits + (orderRes.data.credits_granted || 0), available_credits: prev.available_credits + (orderRes.data.credits_granted || 0) }
          : { total_credits: orderRes.data.credits_granted || 0, used_credits: 0, available_credits: orderRes.data.credits_granted || 0 }
        );
        setPaying(false);
        if (planType === 'email') {
          handleEnrich(activePlan.email_credits);
        } else {
          // linkedin or both — go straight to connect flow
          afterPaymentSuccess(planType);
        }
        return;
      }

      // External checkout (Dodo)
      if (orderRes.data.checkout_url) {
        localStorage.setItem('dodo_pending_plan_id', selectedPlanId);
        setRedirectUrl(orderRes.data.checkout_url);
        setPaying(false);
        window.location.assign(orderRes.data.checkout_url);
        return;
      }

      // Razorpay modal (India)
      const options = {
        key: orderRes.data.key_id,
        amount: orderRes.data.amount,
        currency: orderRes.data.currency || 'INR',
        name: 'Studojo Outreach',
        description: planLabel(activePlan),
        order_id: orderRes.data.order_id,
        handler: async (response: { razorpay_order_id: string; razorpay_payment_id: string; razorpay_signature: string }) => {
          try {
            await api.post('/payment/verify', {
              razorpay_order_id: response.razorpay_order_id,
              razorpay_payment_id: response.razorpay_payment_id,
              razorpay_signature: response.razorpay_signature,
            });
            const cr = await api.get('/payment/credits');
            setCredits(cr.data);
            setPaying(false);
            if (planType === 'email') {
              handleEnrich(activePlan.email_credits);
            } else {
              afterPaymentSuccess(planType);
            }
          } catch (err: any) {
            setError(err.response?.data?.detail || 'Payment verification failed');
            setPaying(false);
          }
        },
        prefill: { email: user?.email || '', name: user?.name || '' },
        theme: { color: '#7C3AED' },
        modal: { ondismiss: () => setPaying(false) },
      };
      const rzp = new window.Razorpay(options);
      rzp.on('payment.failed', (r: any) => { setError(r.error?.description || 'Payment failed'); setPaying(false); });
      rzp.open();
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Failed to create payment order');
      setPaying(false);
    }
  };

  const afterPaymentSuccess = (planType: PlanType) => {
    if (planType === 'linkedin') {
      router.push('/connect/linkedin');
    } else {
      // both — email enrichment first, then chain to LinkedIn after
      router.push('/connect/gmail');
    }
  };

  // Handle Dodo return
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get('dodo_return') !== '1') return;
    const pendingPlanId = localStorage.getItem('dodo_pending_plan_id');
    if (!pendingPlanId) return;

    const verifyDodo = async () => {
      try {
        const sessionId = params.get('session_id') || '';
        if (!sessionId) return;
        const res = await api.post('/payment/verify-dodo', { session_id: sessionId });
        if (res.data.status === 'paid') {
          const plan = allPlans.find((p) => p.plan_id === pendingPlanId);
          const planType = (plan?.plan_type || 'email') as PlanType;
          setPlanType(planType);
          localStorage.removeItem('dodo_pending_plan_id');
          const cr = await api.get('/payment/credits');
          setCredits(cr.data);
          if (planType === 'email' && plan) {
            handleEnrich(plan.email_credits);
          } else if (plan) {
            afterPaymentSuccess(planType);
          }
        }
      } catch { /* show pricing page again */ }
    };
    verifyDodo();
  }, [allPlans]);

  if (loading) return <div className="min-h-screen flex items-center justify-center bg-white"><Spinner /></div>;
  if (!candidateId) { router.push('/onboarding/upload'); return null; }

  const sym = currency === 'INR' ? '₹' : '$';
  const displayPrice = (() => {
    if (!activePlan) return '';
    if (couponResult?.valid) return `${sym}${(couponResult.discounted_amount / 100).toFixed(0)}`;
    return activePlan.display_price;
  })();
  const hasEnoughEmailCredits = activePlan?.plan_type === 'email' && credits
    ? credits.available_credits >= (activePlan.email_credits || 0)
    : false;

  return (
    <div className="min-h-screen bg-white">
      <Navbar />
      <Script src="https://checkout.razorpay.com/v1/checkout.js" strategy="lazyOnload" />
      <Container className="max-w-onboarding py-8">

        {/* Header */}
        <div className="text-center mb-8">
          <h1 className="font-clash text-2xl font-bold">Choose Your Outreach Plan</h1>
          <p className="text-sm text-muted mt-2 font-satoshi">Select how you want to reach out — by email, LinkedIn, or both.</p>
        </div>

        {/* Credit Balance (email) */}
        {credits && credits.total_credits > 0 && (
          <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-4 mb-6 flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="w-9 h-9 rounded-xl bg-studojo-green-bg border-2 border-ink flex items-center justify-center">
                <Coins className="w-5 h-5 text-secondary" />
              </div>
              <div>
                <p className="text-sm font-bold font-satoshi">Email Credits</p>
                <p className="text-xs text-muted font-satoshi">{credits.available_credits} available / {credits.total_credits} total</p>
              </div>
            </div>
            <Badge variant="success">{credits.available_credits} credits</Badge>
          </div>
        )}

        {redirectUrl ? (
          <div className="rounded-2xl border-2 border-ink bg-brand-purple-bg shadow-brutal p-8 text-center">
            <Spinner />
            <p className="text-base font-bold font-satoshi mt-4">Redirecting to payment...</p>
            <a href={redirectUrl} className="text-sm text-primary underline font-satoshi mt-2 block">Click here if not redirected automatically</a>
          </div>
        ) : result ? (
          <ResultCard result={result} router={router} />
        ) : enriching ? (
          <EnrichProgress progress={enrichProgress} />
        ) : (
          <>
            {/* Channel tabs */}
            <div className="flex gap-2 mb-6 rounded-2xl border-2 border-ink bg-surface-muted p-1">
              {([
                { id: 'email', label: 'Send Emails', icon: <Mail className="w-4 h-4" /> },
                { id: 'linkedin', label: 'Send LinkedIn', icon: <Linkedin className="w-4 h-4" /> },
                { id: 'both', label: 'Send Both', icon: <Zap className="w-4 h-4" /> },
              ] as const).map(({ id, label, icon }) => (
                <button
                  key={id}
                  onClick={() => switchChannel(id)}
                  className={`flex-1 flex items-center justify-center gap-2 py-2.5 rounded-xl text-sm font-bold font-satoshi transition-all ${
                    channelTab === id
                      ? 'bg-white border-2 border-ink shadow-brutal text-primary'
                      : 'text-muted hover:text-ink'
                  }`}
                >
                  {icon} {label}
                </button>
              ))}
            </div>

            {/* Plan cards */}
            <div className="grid grid-cols-3 gap-4 mb-6">
              {(visiblePlans.length > 0 ? visiblePlans : fallbackPlans(channelTab, currency)).map((plan) => {
                const isSelected = plan.plan_id === selectedPlanId;
                const savings = BOTH_SAVINGS[plan.plan_id];
                return (
                  <button
                    key={plan.plan_id}
                    onClick={() => selectPlan(plan)}
                    className={`relative rounded-2xl border-2 p-4 text-left transition-all ${
                      isSelected
                        ? 'border-primary bg-brand-purple-bg shadow-brutal'
                        : 'border-ink bg-white hover:border-primary/50'
                    }`}
                  >
                    {savings && (
                      <span className="absolute -top-2.5 left-1/2 -translate-x-1/2 bg-secondary text-white text-[10px] font-bold px-2 py-0.5 rounded-full border border-ink whitespace-nowrap">
                        Save {savings.savePct}%
                      </span>
                    )}
                    <p className="text-xs text-muted font-satoshi mb-1">{plan.label}</p>
                    <p className="text-xl font-bold font-clash">{plan.display_price}</p>
                    {plan.email_credits > 0 && (
                      <p className="text-xs text-muted font-satoshi mt-1">{plan.email_credits} emails</p>
                    )}
                    {plan.linkedin_credits > 0 && (
                      <p className="text-xs text-muted font-satoshi">{plan.linkedin_credits} connections</p>
                    )}
                    {isSelected && (
                      <CheckCircle className="w-4 h-4 text-primary absolute top-3 right-3" />
                    )}
                  </button>
                );
              })}
            </div>

            {/* Channel description */}
            <ChannelDescription tab={channelTab} />

            {/* Coupon */}
            {!hasEnoughEmailCredits && (
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
                        : `${sym}${(couponResult.discount_value / 100).toFixed(0)} off`}
                      {couponResult.distributor && <span className="text-muted font-normal"> via {couponResult.distributor}</span>}
                    </p>
                    <p className="text-xs text-muted font-satoshi mt-1">
                      <span className="line-through">{sym}{(couponResult.original_amount / 100).toFixed(0)}</span>
                      {' → '}
                      <span className="text-secondary font-bold">{sym}{(couponResult.discounted_amount / 100).toFixed(0)}</span>
                    </p>
                  </div>
                )}
              </div>
            )}

            {error && <p className="text-error text-sm text-center mt-6 font-satoshi">{error}</p>}

            <div className="flex flex-col items-center gap-3 mt-8">
              {hasEnoughEmailCredits ? (
                <Button size="lg" onClick={() => activePlan && handleEnrich(activePlan.email_credits)}>
                  <Coins className="w-5 h-5 mr-2 inline" /> Use Credits & Enrich Leads
                </Button>
              ) : (
                <Button size="lg" onClick={handlePayAndProceed} loading={paying}>
                  <CreditCard className="w-5 h-5 mr-2 inline" /> Pay {displayPrice} & Continue
                </Button>
              )}
            </div>
          </>
        )}
      </Container>
    </div>
  );
}

// ── Sub-components ─────────────────────────────────────────────────────────────

function ChannelDescription({ tab }: { tab: ChannelTab }) {
  const descriptions: Record<ChannelTab, { icon: React.ReactNode; text: string }> = {
    email: {
      icon: <Mail className="w-4 h-4 text-primary" />,
      text: 'Personalised cold emails sent from your Gmail account. Best for direct outreach to decision makers.',
    },
    linkedin: {
      icon: <Linkedin className="w-4 h-4 text-primary" />,
      text: 'Automated LinkedIn connection requests with personalised notes. Volume = requests sent (acceptance rate ~20–30%).',
    },
    both: {
      icon: <Zap className="w-4 h-4 text-secondary" />,
      text: 'Email + LinkedIn together — highest coverage, bundled at a discount. Connect both channels after payment.',
    },
  };
  const d = descriptions[tab];
  return (
    <div className="flex items-start gap-3 p-4 rounded-xl bg-surface-muted border border-ink/10 text-sm font-satoshi text-muted">
      <span className="mt-0.5">{d.icon}</span>
      <span>{d.text}</span>
    </div>
  );
}

function ResultCard({ result, router }: { result: { enriched: number; failed: number; planType: PlanType }; router: any }) {
  const nextPath = result.planType === 'linkedin' ? '/connect/linkedin' : '/connect/gmail';
  const nextLabel = result.planType === 'linkedin' ? 'Connect LinkedIn to Send Requests'
    : result.planType === 'both' ? 'Connect Gmail to Continue'
    : 'Connect Gmail to Send Emails';
  return (
    <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-8 text-center animate-fade-in">
      <div className="w-12 h-12 rounded-full bg-studojo-green-bg border-2 border-ink flex items-center justify-center mx-auto mb-6">
        <CheckCircle className="w-6 h-6 text-secondary" />
      </div>
      <h2 className="font-clash text-2xl font-bold mb-2">Enrichment Complete</h2>
      <p className="text-base text-muted font-satoshi">
        <span className="text-secondary font-bold">{result.enriched}</span> emails verified
        {result.failed > 0 && <span className="text-muted"> ({result.failed} not found)</span>}
      </p>
      <Button onClick={() => router.push(nextPath)} className="mt-8">{nextLabel}</Button>
    </div>
  );
}

function EnrichProgress({ progress }: { progress: { enriched: number; failed: number; total: number; progress: string } }) {
  return (
    <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-8">
      <div className="text-center mb-6">
        <p className="text-base text-ink font-bold font-satoshi">Enriching leads...</p>
        <p className="text-sm text-muted font-satoshi mt-1">{progress.progress}</p>
      </div>
      <div className="max-w-md mx-auto space-y-6">
        <div>
          <div className="flex justify-between text-xs text-muted font-satoshi mb-2">
            <span>{progress.enriched} enriched{progress.failed > 0 ? `, ${progress.failed} failed` : ''}</span>
            <span>{progress.total} total</span>
          </div>
          <div className="h-3 rounded-full bg-surface-muted border-2 border-ink/20 overflow-hidden">
            <div
              className="h-full rounded-full bg-primary transition-all duration-500"
              style={{ width: `${progress.total > 0 ? ((progress.enriched + progress.failed) / progress.total) * 100 : 0}%` }}
            />
          </div>
        </div>
        <div className="grid grid-cols-2 gap-4">
          <div className="p-4 rounded-2xl border-2 border-ink bg-studojo-green-bg text-center">
            <p className="text-2xl font-bold font-clash text-secondary">{progress.enriched}</p>
            <p className="text-xs text-muted font-satoshi mt-1">Emails Found</p>
          </div>
          <div className="p-4 rounded-2xl border-2 border-ink/30 bg-white text-center">
            <p className="text-2xl font-bold font-clash text-muted">{progress.failed}</p>
            <p className="text-xs text-muted font-satoshi mt-1">Not Found</p>
          </div>
        </div>
        <div className="flex items-center justify-center gap-3">
          <Spinner size="sm" />
          <span className="text-sm text-muted font-satoshi">This may take a few minutes for large batches</span>
        </div>
      </div>
    </div>
  );
}

function planLabel(plan: PlanPricing): string {
  const parts = [];
  if (plan.email_credits) parts.push(`${plan.email_credits} Email Credits`);
  if (plan.linkedin_credits) parts.push(`${plan.linkedin_credits} LinkedIn Credits`);
  return parts.join(' + ') || plan.label;
}

// Static fallback pricing shown while API loads
function fallbackPlans(type: ChannelTab, currency: string): PlanPricing[] {
  const sym = currency === 'INR' ? '₹' : '$';
  const prices: Record<ChannelTab, [string, string, string]> = {
    email:    [`${sym}20`, `${sym}27`, `${sym}50`],
    linkedin: [`${sym}20`, `${sym}27`, `${sym}50`],
    both:     [`${sym}35`, `${sym}45`, `${sym}70`],
  };
  const vols: [number, number, number] = [200, 350, 500];
  return (['Starter', 'Growth', 'Scale'] as const).map((label, i) => ({
    plan_id: `${type}_${vols[i]}`,
    plan_type: type,
    label,
    email_credits: type !== 'linkedin' ? vols[i] : 0,
    linkedin_credits: type !== 'email' ? vols[i] : 0,
    amount_cents: 0,
    currency,
    display_price: prices[type][i],
  }));
}
