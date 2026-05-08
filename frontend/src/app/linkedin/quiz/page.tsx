'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { Button } from '@/components/ui/Button';
import { ProgressSteps } from '@/components/ui/ProgressSteps';
import { ChevronRight, ChevronLeft } from 'lucide-react';

const INDUSTRIES = ['SaaS / Software', 'Fintech', 'E-commerce', 'Health Tech', 'Ed Tech', 'Climate Tech', 'Media / Content', 'Consulting', 'D2C / Consumer', 'Other'];
const LOCATIONS = ['India', 'United States', 'United Kingdom', 'UAE / Dubai', 'Singapore', 'Europe', 'Southeast Asia', 'Global'];
const COMPANY_SIZES = ['1–10 (pre-seed/seed)', '11–50 (early stage)', '51–200 (Series A/B)', '201–1000 (growth)', '1000+ (enterprise)'];

interface QuizData {
  target_role: string;
  target_industries: string[];
  target_locations: string[];
  target_company_sizes: string[];
  target_keywords: string;
  campaign_name: string;
}

const STEPS = ['Role', 'Industries', 'Location', 'Company size', 'Campaign'];

export default function LinkedInQuizPage() {
  const router = useRouter();
  useAuth();
  const [step, setStep] = useState(0);
  const [data, setData] = useState<QuizData>({
    target_role: '',
    target_industries: [],
    target_locations: [],
    target_company_sizes: [],
    target_keywords: '',
    campaign_name: '',
  });

  const next = () => setStep(s => s + 1);
  const back = () => setStep(s => s - 1);

  const toggleMulti = (field: 'target_industries' | 'target_locations' | 'target_company_sizes', val: string) => {
    setData(d => {
      const arr = d[field];
      return { ...d, [field]: arr.includes(val) ? arr.filter(x => x !== val) : [...arr, val] };
    });
  };

  const handleFinish = () => {
    // Store quiz data in sessionStorage and move to connect step
    sessionStorage.setItem('li_quiz', JSON.stringify(data));
    router.push('/linkedin/connect');
  };

  const canNext = (): boolean => {
    if (step === 0) return data.target_role.trim().length > 2;
    if (step === 1) return data.target_industries.length > 0;
    if (step === 2) return data.target_locations.length > 0;
    if (step === 3) return data.target_company_sizes.length > 0;
    if (step === 4) return data.campaign_name.trim().length > 2;
    return true;
  };

  return (
    <div className="min-h-screen bg-background">
      <Navbar />
      <Container className="py-10 max-w-xl">
        <ProgressSteps steps={STEPS} currentStep={step} className="mb-10" />

        {/* Step 0 — Target role */}
        {step === 0 && (
          <div className="space-y-5">
            <div>
              <h2 className="text-xl font-bold mb-1">Who are you trying to reach?</h2>
              <p className="text-muted-foreground text-sm">Enter the job title or role of your ideal connection.</p>
            </div>
            <input
              type="text"
              value={data.target_role}
              onChange={e => setData(d => ({ ...d, target_role: e.target.value }))}
              placeholder="e.g. Head of Marketing, Founder, VP Sales"
              className="w-full border border-border rounded-xl px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
              autoFocus
            />
            <div className="flex flex-wrap gap-2">
              {['Founder / Co-founder', 'Head of Marketing', 'VP Sales', 'Product Manager', 'CTO', 'HR Manager'].map(r => (
                <button
                  key={r}
                  onClick={() => setData(d => ({ ...d, target_role: r }))}
                  className={`px-3 py-1.5 rounded-full text-xs border transition-colors ${
                    data.target_role === r ? 'bg-primary text-white border-primary' : 'border-border text-muted-foreground hover:border-primary/40'
                  }`}
                >
                  {r}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Step 1 — Industries */}
        {step === 1 && (
          <div className="space-y-5">
            <div>
              <h2 className="text-xl font-bold mb-1">What industries?</h2>
              <p className="text-muted-foreground text-sm">Select all that apply. We'll search within these.</p>
            </div>
            <div className="flex flex-wrap gap-2">
              {INDUSTRIES.map(ind => (
                <button
                  key={ind}
                  onClick={() => toggleMulti('target_industries', ind)}
                  className={`px-3 py-2 rounded-full text-sm border transition-colors ${
                    data.target_industries.includes(ind) ? 'bg-primary text-white border-primary' : 'border-border text-foreground hover:border-primary/40'
                  }`}
                >
                  {ind}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Step 2 — Locations */}
        {step === 2 && (
          <div className="space-y-5">
            <div>
              <h2 className="text-xl font-bold mb-1">Where are they based?</h2>
              <p className="text-muted-foreground text-sm">Pick the markets you want to target.</p>
            </div>
            <div className="flex flex-wrap gap-2">
              {LOCATIONS.map(loc => (
                <button
                  key={loc}
                  onClick={() => toggleMulti('target_locations', loc)}
                  className={`px-3 py-2 rounded-full text-sm border transition-colors ${
                    data.target_locations.includes(loc) ? 'bg-primary text-white border-primary' : 'border-border text-foreground hover:border-primary/40'
                  }`}
                >
                  {loc}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Step 3 — Company size */}
        {step === 3 && (
          <div className="space-y-5">
            <div>
              <h2 className="text-xl font-bold mb-1">What company size?</h2>
              <p className="text-muted-foreground text-sm">Select the stages you're interested in.</p>
            </div>
            <div className="space-y-2">
              {COMPANY_SIZES.map(size => (
                <button
                  key={size}
                  onClick={() => toggleMulti('target_company_sizes', size)}
                  className={`w-full text-left px-4 py-3 rounded-xl border text-sm transition-colors ${
                    data.target_company_sizes.includes(size) ? 'bg-primary/5 border-primary text-foreground font-medium' : 'border-border text-foreground hover:border-primary/40'
                  }`}
                >
                  {size}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Step 4 — Campaign name + keywords */}
        {step === 4 && (
          <div className="space-y-5">
            <div>
              <h2 className="text-xl font-bold mb-1">Name your campaign</h2>
              <p className="text-muted-foreground text-sm">Give it a label so you can track it later.</p>
            </div>
            <input
              type="text"
              value={data.campaign_name}
              onChange={e => setData(d => ({ ...d, campaign_name: e.target.value }))}
              placeholder={`e.g. ${data.target_role} outreach — ${new Date().toLocaleDateString('en', { month: 'short', year: 'numeric' })}`}
              className="w-full border border-border rounded-xl px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
              autoFocus
            />
            <div>
              <label className="text-sm font-medium text-foreground block mb-1.5">Extra keywords <span className="text-muted-foreground font-normal">(optional)</span></label>
              <input
                type="text"
                value={data.target_keywords}
                onChange={e => setData(d => ({ ...d, target_keywords: e.target.value }))}
                placeholder="e.g. YC startup, Series A, AI"
                className="w-full border border-border rounded-xl px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
              />
            </div>
          </div>
        )}

        {/* Nav */}
        <div className="flex items-center justify-between mt-8">
          {step > 0 ? (
            <button onClick={back} className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors">
              <ChevronLeft className="w-4 h-4" /> Back
            </button>
          ) : <div />}

          {step < STEPS.length - 1 ? (
            <Button onClick={next} disabled={!canNext()}>
              Continue <ChevronRight className="w-4 h-4 ml-1" />
            </Button>
          ) : (
            <Button onClick={handleFinish} disabled={!canNext()}>
              Connect LinkedIn <ChevronRight className="w-4 h-4 ml-1" />
            </Button>
          )}
        </div>
      </Container>
    </div>
  );
}
