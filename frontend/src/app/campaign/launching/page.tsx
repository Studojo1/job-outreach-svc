'use client';

import { useEffect, useState, useRef } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { useAppStore } from '@/store/useAppStore';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { Spinner } from '@/components/ui/Spinner';
import { CheckCircle, Shield, Mail, Zap, Clock, Send, AlertCircle } from 'lucide-react';
import api from '@/lib/api';

const stages = [
  { icon: Shield, label: 'Validating campaign configuration', duration: 1500 },
  { icon: CheckCircle, label: 'Checking enriched leads', duration: 2000 },
  { icon: Mail, label: 'Generating personalized emails', duration: 5000 },
  { icon: Clock, label: 'Scheduling send times', duration: 2000 },
  { icon: Zap, label: 'Building send queue', duration: 1500 },
  { icon: Send, label: 'Campaign ready', duration: 1000 },
];

export default function CampaignLaunchingPage() {
  const router = useRouter();
  useAuth();
  const { candidateId, emailAccountId, campaignId, setCampaignId } = useAppStore();
  const [currentStage, setCurrentStage] = useState(0);
  const [error, setError] = useState('');
  const launched = useRef(false);

  useEffect(() => {
    if (!candidateId || !emailAccountId || launched.current) return;
    launched.current = true;

    // Read launch params from sessionStorage (set by setup page)
    const launchData = sessionStorage.getItem('campaign_launch');
    if (!launchData) {
      setError('No campaign configuration found. Please go back to setup.');
      return;
    }

    const { campaignName, selectedStyles, selectedTemplate } = JSON.parse(launchData);

    // Animate stages while API call runs
    const timers: NodeJS.Timeout[] = [];
    let elapsed = 0;
    stages.forEach((stage, i) => {
      elapsed += stage.duration;
      timers.push(setTimeout(() => setCurrentStage(i + 1), elapsed));
    });

    const totalDuration = stages.reduce((sum, s) => sum + s.duration, 0);

    // Actual campaign creation + launch
    const launchCampaign = async () => {
      try {
        // Step 1: Create campaign
        const createRes = await api.post('/campaign/create', {
          candidate_id: candidateId,
          email_account_id: emailAccountId,
          name: campaignName || 'My Outreach Campaign',
          selected_styles: selectedStyles?.length > 0 ? selectedStyles : undefined,
          ...((!selectedStyles || selectedStyles.length === 0) && selectedTemplate && {
            template_id: selectedTemplate.id,
            subject_template: selectedTemplate.subject,
            body_template: selectedTemplate.body,
          }),
        });

        const newCampaignId = createRes.data.campaign_id;
        setCampaignId(newCampaignId);

        // Check if any emails were queued
        if (createRes.data.queued_messages === 0) {
          setError('No leads with verified emails found. Please run lead discovery first.');
          return;
        }

        // Step 2: Transition to running (triggers scheduler)
        await api.post(`/campaign/${newCampaignId}/send`);

        // Navigate to dashboard after animation completes
        setTimeout(() => {
          router.push('/campaign/dashboard');
        }, Math.max(0, totalDuration + 500));

      } catch (err: any) {
        setError(err.response?.data?.detail || 'Campaign launch failed. Please try again.');
      }
    };

    launchCampaign();

    return () => timers.forEach(clearTimeout);
  }, [candidateId, emailAccountId]);

  if (!candidateId) {
    router.push('/onboarding/upload');
    return null;
  }

  return (
    <div className="min-h-screen bg-page">
      <Navbar />
      <Container className="max-w-onboarding py-xl">
        <div className="text-center mb-xxl">
          <h1 className="text-h2">Preparing Your Campaign</h1>
          <p className="text-body-sm text-text-secondary mt-s">
            Setting up your outreach. This takes about 15 seconds.
          </p>
        </div>

        {error ? (
          <div className="card p-xl text-center">
            <AlertCircle className="w-10 h-10 text-error mx-auto mb-m" />
            <p className="text-error text-body-lg mb-l">{error}</p>
            <button
              onClick={() => router.push('/campaign/setup')}
              className="btn-primary"
            >
              Back to Setup
            </button>
          </div>
        ) : (
          <div className="max-w-md mx-auto space-y-l">
            {stages.map((stage, i) => {
              const done = currentStage > i;
              const active = currentStage === i;
              const Icon = stage.icon;

              return (
                <div
                  key={i}
                  className={`flex items-center gap-l p-l rounded-xl border transition-all duration-500 ${
                    done
                      ? 'border-secondary bg-secondary/5'
                      : active
                      ? 'border-primary bg-primary/5 shadow-elevated'
                      : 'border-border-light bg-card'
                  }`}
                >
                  <div className={`w-10 h-10 rounded-lg flex items-center justify-center ${
                    done ? 'bg-secondary text-white' : active ? 'bg-primary/10 text-primary' : 'bg-gray-100 text-text-secondary'
                  }`}>
                    {done ? <CheckCircle className="w-5 h-5" /> : active ? <Spinner size="sm" /> : <Icon className="w-5 h-5" />}
                  </div>
                  <span className={`text-body-sm ${done ? 'text-secondary font-semibold' : active ? 'text-text-primary font-semibold' : 'text-text-secondary'}`}>
                    {stage.label}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </Container>
    </div>
  );
}