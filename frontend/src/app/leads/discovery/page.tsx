'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { useAppStore } from '@/store/useAppStore';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { Spinner } from '@/components/ui/Spinner';
import { CheckCircle, Search, BarChart3, Users, Brain } from 'lucide-react';
import api from '@/lib/api';

const stages = [
  { icon: Brain, label: 'Analyzing your candidate profile', duration: 2000 },
  { icon: Search, label: 'Generating role mapping and filters', duration: 3000 },
  { icon: Users, label: 'Searching for decision makers', duration: 8000 },
  { icon: BarChart3, label: 'Scoring and ranking leads', duration: 4000 },
];

export default function DiscoveryPage() {
  const router = useRouter();
  useAuth();
  const { candidateId } = useAppStore();
  const [currentStage, setCurrentStage] = useState(0);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!candidateId) return;

    // Simulate progress stages while the actual API call runs
    const timers: NodeJS.Timeout[] = [];
    let elapsed = 0;
    stages.forEach((stage, i) => {
      elapsed += stage.duration;
      timers.push(setTimeout(() => setCurrentStage(i + 1), elapsed));
    });

    // Actual API call
    api.post('/discovery/search', { candidate_id: candidateId })
      .then(() => {
        // Ensure we show all stages before navigating
        const totalDuration = stages.reduce((sum, s) => sum + s.duration, 0);
        setTimeout(() => {
          router.push('/leads/results');
        }, Math.max(0, totalDuration - 1000));
      })
      .catch((err) => {
        setError(err.response?.data?.detail || 'Lead discovery failed');
      });

    return () => timers.forEach(clearTimeout);
  }, [candidateId, router]);

  if (!candidateId) {
    router.push('/onboarding/upload');
    return null;
  }

  return (
    <div className="min-h-screen bg-white">
      <Navbar />
      <Container className="max-w-onboarding py-8">
        <div className="text-center mb-12">
          <h1 className="font-clash text-2xl font-bold">Discovering Decision Makers</h1>
          <p className="text-sm text-muted font-satoshi mt-2">
            Sit tight while we find the right people for you.
          </p>
        </div>

        {error ? (
          <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-8 text-center">
            <p className="text-error text-base font-satoshi">{error}</p>
            <button
              onClick={() => window.location.reload()}
              className="inline-flex items-center justify-center font-satoshi font-medium rounded-2xl border-2 border-ink bg-primary text-white shadow-brutal hover:translate-x-[2px] hover:translate-y-[2px] hover:shadow-brutal-active h-12 px-6 text-base mt-6"
            >
              Retry
            </button>
          </div>
        ) : (
          <div className="max-w-md mx-auto space-y-6">
            {stages.map((stage, i) => {
              const done = currentStage > i;
              const active = currentStage === i;
              const Icon = stage.icon;

              return (
                <div
                  key={i}
                  className={`flex items-center gap-6 p-6 rounded-2xl border-2 transition-all duration-500 ${
                    done
                      ? 'border-secondary bg-secondary/5'
                      : active
                      ? 'border-primary bg-primary/5 shadow-brutal-active'
                      : 'border-ink/20 bg-white'
                  }`}
                >
                  <div className={`w-10 h-10 rounded-lg flex items-center justify-center ${
                    done ? 'bg-secondary text-white' : active ? 'bg-primary/10 text-primary' : 'bg-gray-100 text-muted'
                  }`}>
                    {done ? <CheckCircle className="w-5 h-5" /> : active ? <Spinner size="sm" /> : <Icon className="w-5 h-5" />}
                  </div>
                  <span className={`text-sm font-satoshi ${done ? 'text-secondary font-semibold' : active ? 'text-ink font-semibold' : 'text-muted'}`}>
                    {stage.label}
                  </span>
                </div>
              );
            })}

            {/* Skeleton cards */}
            <div className="grid grid-cols-2 gap-4 mt-8">
              {[...Array(4)].map((_, i) => (
                <div
                  key={i}
                  className={`rounded-2xl border-2 border-ink bg-white shadow-brutal p-6 transition-all duration-1000 ${
                    currentStage > 2 ? 'opacity-100' : 'opacity-30'
                  }`}
                >
                  <div className="h-4 bg-gray-200 rounded animate-pulse-soft mb-4 w-3/4" />
                  <div className="h-3 bg-gray-100 rounded animate-pulse-soft mb-2 w-full" />
                  <div className="h-3 bg-gray-100 rounded animate-pulse-soft w-2/3" />
                </div>
              ))}
            </div>
          </div>
        )}
      </Container>
    </div>
  );
}
