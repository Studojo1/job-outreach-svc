'use client';

import { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { useAppStore } from '@/store/useAppStore';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { TierSelector } from '@/components/features/TierSelector';
import { Button } from '@/components/ui/Button';
import { Mail, CheckCircle, Search, ShieldCheck, BadgeCheck } from 'lucide-react';
import api from '@/lib/api';

const ENRICHMENT_STAGES = [
  { icon: Search, label: 'Verifying lead identities', duration: 3000 },
  { icon: Mail, label: 'Finding verified email addresses', duration: 5000 },
  { icon: ShieldCheck, label: 'Validating deliverability', duration: 4000 },
  { icon: BadgeCheck, label: 'Finalizing enrichment results', duration: 2000 },
];

export default function EnrichmentPage() {
  const router = useRouter();
  useAuth();
  const { candidateId, selectedTier, setSelectedTier } = useAppStore();
  const [enriching, setEnriching] = useState(false);
  const [enrichStage, setEnrichStage] = useState(0);
  const [result, setResult] = useState<{ enriched: number; failed: number } | null>(null);
  const [error, setError] = useState('');
  const [enrichCount, setEnrichCount] = useState(0);

  // Animate enrichment stages
  useEffect(() => {
    if (!enriching) return;
    const timers: NodeJS.Timeout[] = [];
    let elapsed = 0;
    ENRICHMENT_STAGES.forEach((stage, i) => {
      elapsed += stage.duration;
      timers.push(setTimeout(() => setEnrichStage(i + 1), elapsed));
    });
    return () => timers.forEach(clearTimeout);
  }, [enriching]);

  const handleEnrich = async (limit: number) => {
    if (!candidateId) return;
    setEnriching(true);
    setEnrichStage(0);
    setEnrichCount(limit);
    setError('');
    try {
      const res = await api.post('/enrichment/enrich', {
        candidate_id: candidateId,
        limit,
      });
      setResult(res.data);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Enrichment failed');
    } finally {
      setEnriching(false);
    }
  };

  if (!candidateId) {
    router.push('/onboarding/upload');
    return null;
  }

  return (
    <div className="min-h-screen bg-page">
      <Navbar />
      <Container className="max-w-onboarding py-xl">
        <div className="text-center mb-xl">
          <div className="w-14 h-14 rounded-xl bg-primary/10 flex items-center justify-center mx-auto text-primary mb-l">
            <Mail className="w-7 h-7" />
          </div>
          <h1 className="text-h2">Email Enrichment</h1>
          <p className="text-body-sm text-text-secondary mt-s">
            Choose how many leads to enrich with verified email addresses.
          </p>
        </div>

        {result ? (
          <div className="card p-xl text-center animate-fade-in">
            <CheckCircle className="w-12 h-12 text-secondary mx-auto mb-l" />
            <h2 className="text-h2 mb-s">Enrichment Complete</h2>
            <p className="text-body-lg text-text-secondary">
              <span className="text-secondary font-bold">{result.enriched}</span> emails verified
              {result.failed > 0 && (
                <span className="text-text-secondary"> ({result.failed} not found)</span>
              )}
            </p>
            <Button onClick={() => router.push('/connect/gmail')} className="mt-xl">
              Connect Gmail to Send Emails
            </Button>
          </div>
        ) : enriching ? (
          <div className="card p-xl">
            <p className="text-body-lg text-text-primary font-semibold text-center mb-6">
              Enriching {enrichCount} leads...
            </p>
            <div className="max-w-md mx-auto space-y-4">
              {ENRICHMENT_STAGES.map((stage, i) => {
                const done = enrichStage > i;
                const active = enrichStage === i;
                const Icon = stage.icon;
                return (
                  <div
                    key={i}
                    className={`flex items-center gap-4 p-4 rounded-xl border transition-all duration-500 ${
                      done
                        ? 'border-green-300 bg-green-50'
                        : active
                        ? 'border-primary bg-primary/5 shadow-md'
                        : 'border-border-light bg-card'
                    }`}
                  >
                    <div className={`w-9 h-9 rounded-lg flex items-center justify-center ${
                      done ? 'bg-green-500 text-white' : active ? 'bg-primary/10 text-primary' : 'bg-gray-100 text-text-secondary'
                    }`}>
                      {done ? <CheckCircle className="w-5 h-5" /> : <Icon className="w-5 h-5" />}
                    </div>
                    <span className={`text-sm ${done ? 'text-green-700 font-semibold' : active ? 'text-text-primary font-semibold' : 'text-text-secondary'}`}>
                      {stage.label}
                    </span>
                    {active && (
                      <div className="ml-auto w-4 h-4 border-2 border-primary border-t-transparent rounded-full animate-spin" />
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        ) : (
          <>
            <TierSelector selected={selectedTier} onSelect={setSelectedTier} />

            {error && (
              <p className="text-error text-body-sm text-center mt-l">{error}</p>
            )}

            <div className="flex flex-col items-center gap-3 mt-xl">
              <Button size="lg" onClick={() => handleEnrich(selectedTier)}>
                Enrich {selectedTier} Leads
              </Button>
            </div>
          </>
        )}
      </Container>
    </div>
  );
}