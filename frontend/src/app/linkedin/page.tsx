'use client';

import { useState, useEffect, useRef } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { ProgressSteps } from '@/components/ui/ProgressSteps';
import { Button } from '@/components/ui/Button';
import { Spinner } from '@/components/ui/Spinner';
import {
  ChevronRight, ChevronLeft, Eye, EyeOff, ShieldCheck,
  AlertCircle, ExternalLink, CheckCircle, Users, MessageSquare,
  Send, Pause, Play, ThumbsUp, ThumbsDown, Minus, Linkedin, Search,
} from 'lucide-react';
import api from '@/lib/api';
import type { LinkedInCampaign, CampaignStats, ConnectionRequest } from '@/lib/types/linkedin';

// ── Types ─────────────────────────────────────────────────────────────────────

type Step = 1 | 2 | 3 | 4 | 5 | 6;

interface QuizData {
  target_role: string;
  target_industries: string[];
  target_locations: string[];
  target_company_sizes: string[];
  target_keywords: string;
  campaign_name: string;
}

// ── Constants ─────────────────────────────────────────────────────────────────

const STEPS = ['Target', 'Industries', 'Location', 'Connect', 'Messages', 'Live'];

const INDUSTRIES = [
  'SaaS / Software', 'Fintech', 'E-commerce', 'Health Tech',
  'Ed Tech', 'Climate Tech', 'Media / Content', 'Consulting',
  'D2C / Consumer', 'AI / ML',
];

const LOCATIONS = [
  'India', 'United States', 'United Kingdom',
  'UAE / Dubai', 'Singapore', 'Europe', 'Southeast Asia', 'Global',
];

const COMPANY_SIZES = [
  '1–10 (pre-seed/seed)',
  '11–50 (early stage)',
  '51–200 (Series A/B)',
  '201–1000 (growth)',
  '1000+ (enterprise)',
];

const ROLE_SUGGESTIONS = [
  'Founder / Co-founder', 'Head of Marketing', 'VP Sales',
  'Product Manager', 'CTO', 'HR Manager', 'Chief of Staff',
];

const NOTE_PLACEHOLDER = `Hi {{name}}, came across your work at {{company}} — really interesting what you're building. Would love to connect!`;
const FOLLOWUP_PLACEHOLDER = `Hey {{name}}, thanks for connecting! I'm a student really interested in {{role}} and what you're doing at {{company}}. Would love to chat for 15 min if you have time.`;

const STATUS_LABELS: Record<string, { label: string; color: string }> = {
  pending:       { label: 'Queued',        color: 'bg-gray-100 text-gray-500' },
  sent:          { label: 'Sent',           color: 'bg-blue-100 text-blue-700' },
  accepted:      { label: 'Accepted',       color: 'bg-green-100 text-green-700' },
  followup_sent: { label: 'Follow-up sent', color: 'bg-violet-100 text-violet-700' },
  replied:       { label: 'Replied',        color: 'bg-emerald-100 text-emerald-700' },
  error:         { label: 'Error',          color: 'bg-red-100 text-red-600' },
};

// ── Sub-components ─────────────────────────────────────────────────────────────

function Chip({
  label, selected, onClick,
}: { label: string; selected: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`px-3 py-2 rounded-full text-sm border transition-all ${
        selected
          ? 'bg-primary text-white border-primary font-medium'
          : 'border-border text-foreground hover:border-primary/50 bg-white'
      }`}
    >
      {label}
    </button>
  );
}

function StepNav({
  onBack, onNext, nextLabel = 'Continue', disabled = false, hideBack = false,
}: {
  onBack?: () => void;
  onNext: () => void;
  nextLabel?: string;
  disabled?: boolean;
  hideBack?: boolean;
}) {
  return (
    <div className="flex items-center justify-between mt-8">
      {!hideBack && onBack ? (
        <button
          onClick={onBack}
          className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
        >
          <ChevronLeft className="w-4 h-4" /> Back
        </button>
      ) : <div />}
      <Button onClick={onNext} disabled={disabled}>
        {nextLabel} <ChevronRight className="w-4 h-4 ml-1" />
      </Button>
    </div>
  );
}

function MetricTile({ label, value, sub }: { label: string; value: number | string; sub?: string }) {
  return (
    <div className="bg-white border border-border rounded-xl p-4 text-center">
      <p className="text-2xl font-bold text-foreground">{value}</p>
      <p className="text-xs text-muted-foreground mt-0.5">{label}</p>
      {sub && <p className="text-xs text-primary font-medium mt-1">{sub}</p>}
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────────────

export default function LinkedInOnboardingPage() {
  const router = useRouter();
  useAuth();

  const [step, setStep] = useState<Step>(1);

  // Quiz state
  const [quiz, setQuiz] = useState<QuizData>({
    target_role: '',
    target_industries: [],
    target_locations: [],
    target_company_sizes: [],
    target_keywords: '',
    campaign_name: '',
  });

  // LinkedIn connect state
  const [liEmail, setLiEmail] = useState('');
  const [liPassword, setLiPassword] = useState('');
  const [showPass, setShowPass] = useState(false);
  const [connectLoading, setConnectLoading] = useState(false);
  const [connectError, setConnectError] = useState('');
  const [challengeRequired, setChallengeRequired] = useState(false);
  const [sessionKey, setSessionKey] = useState('');
  const [pin, setPin] = useState('');
  const [pinLoading, setPinLoading] = useState(false);
  const [loginTab, setLoginTab] = useState<'password' | 'cookies'>('password');
  const [liAtCookie, setLiAtCookie] = useState('');
  const [jsessionidCookie, setJsessionidCookie] = useState('');
  const [cookieLoading, setCookieLoading] = useState(false);
  const [extInstalled, setExtInstalled] = useState(false);
  const [extLoading, setExtLoading] = useState(false);

  // Messages state
  const [connectionNote, setConnectionNote] = useState('');
  const [followupMessage, setFollowupMessage] = useState('');
  const [dailyLimit, setDailyLimit] = useState(20);

  // Campaign + leads state
  const [campaignId, setCampaignId] = useState<number | null>(null);
  const [leads, setLeads] = useState<ConnectionRequest[]>([]);
  const [searchingLeads, setSearchingLeads] = useState(false);
  const [leadsError, setLeadsError] = useState('');
  const [launching, setLaunching] = useState(false);
  const [launchError, setLaunchError] = useState('');

  // Dashboard state
  const [campaign, setCampaign] = useState<LinkedInCampaign | null>(null);
  const [stats, setStats] = useState<CampaignStats | null>(null);
  const [requests, setRequests] = useState<ConnectionRequest[]>([]);
  const [toggling, setToggling] = useState(false);
  const [activeTab, setActiveTab] = useState<'all' | 'sent' | 'accepted' | 'replied'>('all');

  const pollRef = useRef<NodeJS.Timeout | null>(null);

  // Toggle helpers
  const toggleMulti = (field: 'target_industries' | 'target_locations' | 'target_company_sizes', val: string) => {
    setQuiz(q => {
      const arr = q[field];
      return { ...q, [field]: arr.includes(val) ? arr.filter(x => x !== val) : [...arr, val] };
    });
  };

  // Step validity
  const canNext = (): boolean => {
    if (step === 1) return quiz.target_role.trim().length > 1;
    if (step === 2) return quiz.target_industries.length > 0;
    if (step === 3) return quiz.target_locations.length > 0 && quiz.target_company_sizes.length > 0 && quiz.campaign_name.trim().length > 1;
    if (step === 4) return liEmail.length > 0 && liPassword.length > 0;
    return true;
  };

  const next = () => setStep(s => (s + 1) as Step);
  const back = () => setStep(s => (s - 1) as Step);

  // Step 4 → connect + search leads
  const proceedAfterLogin = async () => {
    const res = await api.post('/linkedin/automation/campaigns', {
      name: quiz.campaign_name || `${quiz.target_role} outreach`,
      target_role: quiz.target_role,
      target_industries: quiz.target_industries,
      target_locations: quiz.target_locations,
      target_company_sizes: quiz.target_company_sizes,
      target_keywords: quiz.target_keywords || null,
      daily_limit: dailyLimit,
    });
    const id = res.data.id;
    setCampaignId(id);
    setStep(5);
    setSearchingLeads(true);
    await api.post(`/linkedin/automation/campaigns/${id}/search-leads`);
    let attempts = 0;
    const poll = async () => {
      try {
        const r = await api.get(`/linkedin/automation/campaigns/${id}/requests?limit=50`);
        if (r.data.length > 0) { setLeads(r.data); setSearchingLeads(false); return; }
      } catch {}
      attempts++;
      if (attempts < 20) pollRef.current = setTimeout(poll, 3000);
      else { setSearchingLeads(false); setLeadsError('No leads found. Try broadening your search criteria.'); }
    };
    poll();
  };

  const handleConnect = async () => {
    setConnectLoading(true);
    setConnectError('');
    try {
      const res = await api.post('/linkedin/automation/login', { email: liEmail, password: liPassword });
      if (res.data.challenge_required) {
        setSessionKey(res.data.session_key);
        setChallengeRequired(true);
        return;
      }
      await proceedAfterLogin();
    } catch (err: any) {
      setConnectError(err.response?.data?.detail || 'Connection failed. Check your credentials.');
    } finally {
      setConnectLoading(false);
    }
  };

  const handleVerifyPin = async () => {
    setPinLoading(true);
    setConnectError('');
    try {
      await api.post('/linkedin/automation/login/verify-pin', { session_key: sessionKey, pin });
      setChallengeRequired(false);
      await proceedAfterLogin();
    } catch (err: any) {
      setConnectError(err.response?.data?.detail || 'Incorrect code. Please try again.');
    } finally {
      setPinLoading(false);
    }
  };

  const handleCookieLogin = async () => {
    setCookieLoading(true);
    setConnectError('');
    try {
      await api.post('/linkedin/automation/login/cookies', {
        li_at: liAtCookie.trim(),
        jsessionid: jsessionidCookie.trim(),
      });
      await proceedAfterLogin();
    } catch (err: any) {
      setConnectError(err.response?.data?.detail || 'Invalid cookies. Please check and try again.');
    } finally {
      setCookieLoading(false);
    }
  };

  const handleExtensionLogin = () => {
    setExtLoading(true);
    setConnectError('');

    const onCookies = (e: Event) => {
      const { li_at, jsessionid, error } = (e as CustomEvent).detail || {};
      window.removeEventListener('STUDOJO_LI_COOKIES', onCookies);
      if (error || !li_at) {
        setConnectError(error || 'Extension could not read LinkedIn cookies. Make sure you\'re logged in to LinkedIn.');
        setExtLoading(false);
        return;
      }
      api.post('/linkedin/automation/login/cookies', { li_at, jsessionid: jsessionid || '' })
        .then(() => proceedAfterLogin())
        .catch((err: any) => {
          setConnectError(err.response?.data?.detail || 'LinkedIn session invalid. Please re-login to LinkedIn and try again.');
        })
        .finally(() => setExtLoading(false));
    };

    window.addEventListener('STUDOJO_LI_COOKIES', onCookies);
    window.dispatchEvent(new CustomEvent('STUDOJO_REQUEST_LI_COOKIES'));

    // Timeout if extension doesn't respond
    setTimeout(() => {
      window.removeEventListener('STUDOJO_LI_COOKIES', onCookies);
      if (extLoading) {
        setConnectError('Extension did not respond. Try refreshing or installing the extension.');
        setExtLoading(false);
      }
    }, 5000);
  };

  // Step 6 → launch
  const handleLaunch = async () => {
    if (!campaignId) return;
    setLaunching(true);
    setLaunchError('');
    try {
      await api.put(`/linkedin/automation/campaigns/${campaignId}`, {
        name: quiz.campaign_name || `${quiz.target_role} outreach`,
        target_role: quiz.target_role,
        target_industries: quiz.target_industries,
        target_locations: quiz.target_locations,
        target_company_sizes: quiz.target_company_sizes,
        target_keywords: quiz.target_keywords || null,
        connection_note: connectionNote || NOTE_PLACEHOLDER,
        followup_message: followupMessage || FOLLOWUP_PLACEHOLDER,
        daily_limit: dailyLimit,
      });
      await api.post(`/linkedin/automation/campaigns/${campaignId}/launch`);

      // Load dashboard data
      await fetchDashboard(campaignId);
      setStep(6);

      // Auto-refresh every 30s
      pollRef.current = setInterval(() => fetchDashboard(campaignId), 30000);
    } catch (err: any) {
      setLaunchError(err.response?.data?.detail || 'Launch failed. Please try again.');
    } finally {
      setLaunching(false);
    }
  };

  const fetchDashboard = async (id: number) => {
    const [cRes, sRes, rRes] = await Promise.all([
      api.get(`/linkedin/automation/campaigns/${id}`),
      api.get(`/linkedin/automation/campaigns/${id}/stats`),
      api.get(`/linkedin/automation/campaigns/${id}/requests?limit=200`),
    ]);
    setCampaign(cRes.data);
    setStats(sRes.data);
    setRequests(rRes.data);
  };

  const toggleCampaign = async () => {
    if (!campaign || !campaignId) return;
    setToggling(true);
    try {
      const action = campaign.status === 'running' ? 'pause' : 'resume';
      await api.post(`/linkedin/automation/campaigns/${campaignId}/${action}`);
      await fetchDashboard(campaignId);
    } finally {
      setToggling(false);
    }
  };

  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  // Detect Studojo extension
  useEffect(() => {
    const onReady = () => setExtInstalled(true);
    window.addEventListener('STUDOJO_EXT_READY', onReady);
    // Give content script 500ms to fire the ready event
    const t = setTimeout(() => window.removeEventListener('STUDOJO_EXT_READY', onReady), 500);
    return () => { clearTimeout(t); window.removeEventListener('STUDOJO_EXT_READY', onReady); };
  }, []);

  const filteredRequests = requests.filter(r => {
    if (activeTab === 'sent') return ['sent', 'accepted', 'followup_sent', 'replied'].includes(r.status);
    if (activeTab === 'accepted') return ['accepted', 'followup_sent', 'replied'].includes(r.status);
    if (activeTab === 'replied') return r.status === 'replied';
    return true;
  });

  // ── Render steps ──────────────────────────────────────────────────────────────

  return (
    <div className="min-h-screen bg-background">
      <Navbar />
      <Container className="py-10 max-w-xl">

        {/* Progress */}
        <div className="mb-10">
          <ProgressSteps steps={STEPS} currentStep={step} />
        </div>

        {/* ── Step 1: Target role ─────────────────────────────────────── */}
        {step === 1 && (
          <div>
            <h2 className="text-xl font-bold mb-1">Who do you want to reach?</h2>
            <p className="text-sm text-muted-foreground mb-6">
              Enter the job title of your ideal LinkedIn connection.
            </p>
            <input
              type="text"
              value={quiz.target_role}
              onChange={e => setQuiz(q => ({ ...q, target_role: e.target.value }))}
              placeholder="e.g. Head of Marketing, Founder, VP Sales"
              className="w-full border border-border rounded-xl px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30 mb-4"
              autoFocus
            />
            <div className="flex flex-wrap gap-2">
              {ROLE_SUGGESTIONS.map(r => (
                <Chip
                  key={r} label={r} selected={quiz.target_role === r}
                  onClick={() => setQuiz(q => ({ ...q, target_role: r }))}
                />
              ))}
            </div>
            <StepNav hideBack onNext={next} disabled={!canNext()} />
          </div>
        )}

        {/* ── Step 2: Industries ──────────────────────────────────────── */}
        {step === 2 && (
          <div>
            <h2 className="text-xl font-bold mb-1">What industries?</h2>
            <p className="text-sm text-muted-foreground mb-6">Select all that apply.</p>
            <div className="flex flex-wrap gap-2">
              {INDUSTRIES.map(ind => (
                <Chip
                  key={ind} label={ind}
                  selected={quiz.target_industries.includes(ind)}
                  onClick={() => toggleMulti('target_industries', ind)}
                />
              ))}
            </div>
            <StepNav onBack={back} onNext={next} disabled={!canNext()} />
          </div>
        )}

        {/* ── Step 3: Location + company size + name ───────────────────── */}
        {step === 3 && (
          <div className="space-y-6">
            <div>
              <h2 className="text-xl font-bold mb-1">Where are they based?</h2>
              <p className="text-sm text-muted-foreground mb-4">Pick your target markets.</p>
              <div className="flex flex-wrap gap-2">
                {LOCATIONS.map(loc => (
                  <Chip
                    key={loc} label={loc}
                    selected={quiz.target_locations.includes(loc)}
                    onClick={() => toggleMulti('target_locations', loc)}
                  />
                ))}
              </div>
            </div>

            <div>
              <p className="text-sm font-medium mb-3">Company size</p>
              <div className="space-y-2">
                {COMPANY_SIZES.map(size => (
                  <button
                    key={size}
                    onClick={() => toggleMulti('target_company_sizes', size)}
                    className={`w-full text-left px-4 py-3 rounded-xl border text-sm transition-all ${
                      quiz.target_company_sizes.includes(size)
                        ? 'bg-primary/5 border-primary font-medium'
                        : 'border-border hover:border-primary/40'
                    }`}
                  >
                    {size}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <label className="text-sm font-medium block mb-1.5">Campaign name</label>
              <input
                type="text"
                value={quiz.campaign_name}
                onChange={e => setQuiz(q => ({ ...q, campaign_name: e.target.value }))}
                placeholder={`${quiz.target_role} outreach — ${new Date().toLocaleDateString('en', { month: 'short', year: 'numeric' })}`}
                className="w-full border border-border rounded-xl px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
              />
            </div>

            <div>
              <label className="text-sm font-medium block mb-1.5">
                Extra keywords <span className="font-normal text-muted-foreground">(optional)</span>
              </label>
              <input
                type="text"
                value={quiz.target_keywords}
                onChange={e => setQuiz(q => ({ ...q, target_keywords: e.target.value }))}
                placeholder="e.g. Series A startup, YC, AI"
                className="w-full border border-border rounded-xl px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
              />
            </div>

            <StepNav onBack={back} onNext={next} disabled={!canNext()} />
          </div>
        )}

        {/* ── Step 4: Connect LinkedIn ────────────────────────────────── */}
        {step === 4 && (
          <div>
            <div className="flex items-center gap-3 mb-6">
              <div className="w-10 h-10 bg-[#0077B5] rounded-xl flex items-center justify-center">
                <Linkedin className="w-5 h-5 text-white" />
              </div>
              <div>
                <h2 className="text-xl font-bold">
                  {challengeRequired ? 'Check your email' : 'Connect your LinkedIn'}
                </h2>
                <p className="text-sm text-muted-foreground">
                  {challengeRequired
                    ? `LinkedIn sent a verification code to ${liEmail}`
                    : "We'll search for leads and send requests on your behalf."}
                </p>
              </div>
            </div>

            {/* Tab switcher — only show when not in challenge flow */}
            {!challengeRequired && (
              <div className="flex border border-border rounded-xl overflow-hidden mb-5">
                <button
                  onClick={() => { setLoginTab('password'); setConnectError(''); }}
                  className={`flex-1 py-2.5 text-sm font-medium transition-colors ${loginTab === 'password' ? 'bg-primary text-white' : 'bg-white text-muted-foreground hover:text-foreground'}`}
                >
                  Email & password
                </button>
                <button
                  onClick={() => { setLoginTab('cookies'); setConnectError(''); }}
                  className={`flex-1 py-2.5 text-sm font-medium transition-colors ${loginTab === 'cookies' ? 'bg-primary text-white' : 'bg-white text-muted-foreground hover:text-foreground'}`}
                >
                  Use extension
                </button>
              </div>
            )}

            {!challengeRequired ? (
              <>
                {loginTab === 'password' ? (
                  <>
                    <div className="bg-white border border-border rounded-2xl p-5 space-y-4 mb-5">
                      <div>
                        <label className="text-sm font-medium block mb-1.5">LinkedIn email</label>
                        <input type="email" value={liEmail} onChange={e => setLiEmail(e.target.value)}
                          placeholder="you@example.com"
                          className="w-full border border-border rounded-xl px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
                          autoComplete="email" />
                      </div>
                      <div>
                        <label className="text-sm font-medium block mb-1.5">LinkedIn password</label>
                        <div className="relative">
                          <input type={showPass ? 'text' : 'password'} value={liPassword}
                            onChange={e => setLiPassword(e.target.value)} placeholder="••••••••"
                            className="w-full border border-border rounded-xl px-4 py-3 pr-10 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
                            onKeyDown={e => e.key === 'Enter' && canNext() && handleConnect()} />
                          <button type="button" onClick={() => setShowPass(v => !v)}
                            className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground">
                            {showPass ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                          </button>
                        </div>
                      </div>
                      {connectError && (
                        <div className="flex items-start gap-2 bg-red-50 border border-red-200 rounded-xl p-3 text-sm text-red-700">
                          <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" /><span>{connectError}</span>
                        </div>
                      )}
                    </div>
                    <div className="space-y-2 mb-6">
                      {['Credentials encrypted with AES-256 before storage', 'Only used to send connection requests you configure', 'Disconnect at any time'].map(item => (
                        <div key={item} className="flex items-center gap-2.5 text-sm text-muted-foreground">
                          <ShieldCheck className="w-4 h-4 text-green-600 flex-shrink-0" /><span>{item}</span>
                        </div>
                      ))}
                    </div>
                    <div className="flex items-center justify-between">
                      <button onClick={back} className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
                        <ChevronLeft className="w-4 h-4" /> Back
                      </button>
                      <Button onClick={handleConnect} disabled={!canNext() || connectLoading}>
                        {connectLoading ? 'Connecting...' : 'Connect & find leads'}<ChevronRight className="w-4 h-4 ml-1" />
                      </Button>
                    </div>
                  </>
                ) : (
                  <>
                    {extInstalled ? (
                      /* Extension detected — one-click flow */
                      <div className="space-y-4">
                        <div className="bg-white border border-border rounded-2xl p-6 text-center space-y-3">
                          <div className="w-12 h-12 bg-primary/10 rounded-full flex items-center justify-center mx-auto">
                            <ShieldCheck className="w-6 h-6 text-primary" />
                          </div>
                          <p className="font-medium text-sm">Studojo extension detected</p>
                          <p className="text-xs text-muted-foreground">
                            Click below and the extension will securely read your LinkedIn session — no copy-pasting needed.
                          </p>
                          {connectError && (
                            <div className="flex items-start gap-2 bg-red-50 border border-red-200 rounded-xl p-3 text-sm text-red-700 text-left">
                              <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" /><span>{connectError}</span>
                            </div>
                          )}
                          <Button onClick={handleExtensionLogin} disabled={extLoading} className="w-full mt-1">
                            {extLoading ? 'Reading cookies...' : 'Connect via extension'}
                            {!extLoading && <ChevronRight className="w-4 h-4 ml-1" />}
                          </Button>
                        </div>
                        <p className="text-center text-xs text-muted-foreground">
                          Make sure you&apos;re logged in to{' '}
                          <a href="https://www.linkedin.com" target="_blank" rel="noreferrer" className="underline">linkedin.com</a>{' '}
                          in this browser first.
                        </p>
                        <div className="flex items-center justify-between pt-1">
                          <button onClick={back} className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
                            <ChevronLeft className="w-4 h-4" /> Back
                          </button>
                          <button
                            onClick={() => setExtInstalled(false)}
                            className="text-xs text-muted-foreground hover:text-foreground underline"
                          >
                            Paste manually instead
                          </button>
                        </div>
                      </div>
                    ) : (
                      /* No extension — show install prompt + manual paste fallback */
                      <div className="space-y-4">
                        <div className="bg-primary/5 border border-primary/20 rounded-2xl p-5 space-y-3">
                          <p className="font-medium text-sm">Install the Studojo extension</p>
                          <p className="text-xs text-muted-foreground">
                            The extension reads your LinkedIn session in one click — no DevTools needed.
                          </p>
                          <a
                            href="/studojo-linkedin-connector.zip"
                            download
                            className="inline-flex items-center gap-2 text-sm text-primary font-medium hover:underline"
                          >
                            Download extension (.zip)
                          </a>
                          <p className="text-xs text-muted-foreground">
                            Then open <strong>chrome://extensions</strong> → Enable Developer mode → Load unpacked → select the folder.
                          </p>
                        </div>

                        <div className="border-t border-border pt-4">
                          <p className="text-xs text-muted-foreground mb-3 font-medium">Or paste cookies manually:</p>
                          <div className="bg-blue-50 border border-blue-200 rounded-xl p-3 mb-3 text-xs text-blue-700 space-y-1">
                            <p>1. Open <strong>linkedin.com</strong> and log in</p>
                            <p>2. Press <strong>F12</strong> → Application → Cookies → linkedin.com</p>
                            <p>3. Copy <strong>li_at</strong> and <strong>JSESSIONID</strong> values below</p>
                          </div>
                          <div className="bg-white border border-border rounded-2xl p-4 space-y-3">
                            <div>
                              <label className="text-xs font-medium block mb-1">li_at cookie</label>
                              <textarea value={liAtCookie} onChange={e => setLiAtCookie(e.target.value)}
                                placeholder="AQEDATxxxxxx..."
                                rows={2}
                                className="w-full border border-border rounded-xl px-3 py-2 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-primary/30 resize-none" />
                            </div>
                            <div>
                              <label className="text-xs font-medium block mb-1">JSESSIONID cookie</label>
                              <input type="text" value={jsessionidCookie} onChange={e => setJsessionidCookie(e.target.value)}
                                placeholder="ajax:xxxxxxxxxx"
                                className="w-full border border-border rounded-xl px-3 py-2 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-primary/30" />
                            </div>
                            {connectError && (
                              <div className="flex items-start gap-2 bg-red-50 border border-red-200 rounded-xl p-3 text-sm text-red-700">
                                <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" /><span>{connectError}</span>
                              </div>
                            )}
                          </div>
                        </div>

                        <div className="flex items-center justify-between">
                          <button onClick={back} className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
                            <ChevronLeft className="w-4 h-4" /> Back
                          </button>
                          <Button onClick={handleCookieLogin} disabled={!liAtCookie.trim() || !jsessionidCookie.trim() || cookieLoading}>
                            {cookieLoading ? 'Connecting...' : 'Connect & find leads'}<ChevronRight className="w-4 h-4 ml-1" />
                          </Button>
                        </div>
                      </div>
                    )}
                  </>
                )}
              </>
            ) : (
              <>
                <div className="bg-white border border-border rounded-2xl p-5 space-y-4 mb-5">
                  <div>
                    <label className="text-sm font-medium block mb-1.5">Verification code</label>
                    <input
                      type="text"
                      inputMode="numeric"
                      value={pin}
                      onChange={e => setPin(e.target.value.replace(/\D/g, '').slice(0, 8))}
                      placeholder="Enter the code LinkedIn emailed you"
                      className="w-full border border-border rounded-xl px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30 tracking-widest text-center text-lg font-mono"
                      autoFocus
                      onKeyDown={e => e.key === 'Enter' && pin.length >= 4 && handleVerifyPin()}
                    />
                  </div>

                  {connectError && (
                    <div className="flex items-start gap-2 bg-red-50 border border-red-200 rounded-xl p-3 text-sm text-red-700">
                      <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
                      <span>{connectError}</span>
                    </div>
                  )}
                </div>

                <div className="flex items-center justify-between">
                  <button
                    onClick={() => { setChallengeRequired(false); setPin(''); setConnectError(''); }}
                    className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
                  >
                    <ChevronLeft className="w-4 h-4" /> Back
                  </button>
                  <Button onClick={handleVerifyPin} disabled={pin.length < 4 || pinLoading}>
                    {pinLoading ? 'Verifying...' : 'Verify & continue'}
                    <ChevronRight className="w-4 h-4 ml-1" />
                  </Button>
                </div>

                <div className="mt-4 pt-4 border-t border-border text-center">
                  <p className="text-xs text-muted-foreground mb-2">Code not arriving?</p>
                  <button
                    onClick={() => { setChallengeRequired(false); setLoginTab('cookies'); setPin(''); setConnectError(''); }}
                    className="text-sm text-primary hover:underline font-medium"
                  >
                    Try another way — use the extension instead
                  </button>
                </div>
              </>
            )}
          </div>
        )}

        {/* ── Step 5: Leads + messages ────────────────────────────────── */}
        {step === 5 && (
          <div>
            {/* Leads found section */}
            <div className="mb-6">
              {searchingLeads ? (
                <div className="bg-white border border-border rounded-2xl p-6 text-center">
                  <div className="w-10 h-10 bg-primary/10 rounded-full flex items-center justify-center mx-auto mb-3">
                    <Search className="w-5 h-5 text-primary animate-pulse" />
                  </div>
                  <p className="font-medium text-sm">Finding {quiz.target_role}s...</p>
                  <p className="text-xs text-muted-foreground mt-1">Searching LinkedIn for your target profiles</p>
                </div>
              ) : leadsError ? (
                <div className="bg-red-50 border border-red-200 rounded-xl p-4 text-sm text-red-700">{leadsError}</div>
              ) : (
                <div className="bg-white border border-border rounded-2xl overflow-hidden">
                  <div className="px-4 py-3 border-b border-border flex items-center gap-2">
                    <Users className="w-4 h-4 text-primary" />
                    <span className="text-sm font-medium">{leads.length} leads found</span>
                  </div>
                  <div className="divide-y divide-border max-h-48 overflow-y-auto">
                    {leads.slice(0, 10).map(lead => (
                      <div key={lead.id} className="px-4 py-2.5 flex items-center justify-between gap-3">
                        <div className="min-w-0">
                          <p className="text-sm font-medium truncate">{lead.name}</p>
                          {lead.headline && <p className="text-xs text-muted-foreground truncate">{lead.headline}</p>}
                        </div>
                        <a href={lead.profile_url} target="_blank" rel="noopener noreferrer" className="text-muted-foreground hover:text-foreground flex-shrink-0">
                          <ExternalLink className="w-3 h-3" />
                        </a>
                      </div>
                    ))}
                    {leads.length > 10 && (
                      <div className="px-4 py-2 text-xs text-center text-muted-foreground">
                        + {leads.length - 10} more leads
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>

            {/* Message templates */}
            <h2 className="text-lg font-bold mb-4">Set up your messages</h2>

            <div className="space-y-4 mb-6">
              <div className="bg-white border border-border rounded-2xl p-4">
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2">
                    <MessageSquare className="w-4 h-4 text-primary" />
                    <span className="text-sm font-medium">Connection note</span>
                  </div>
                  <span className={`text-xs ${connectionNote.length > 300 ? 'text-red-500' : 'text-muted-foreground'}`}>
                    {connectionNote.length}/300
                  </span>
                </div>
                <textarea
                  value={connectionNote}
                  onChange={e => setConnectionNote(e.target.value)}
                  placeholder={NOTE_PLACEHOLDER}
                  rows={2}
                  className="w-full text-sm border border-border rounded-xl px-3 py-2.5 resize-none focus:outline-none focus:ring-2 focus:ring-primary/30"
                />
                <p className="text-xs text-muted-foreground mt-1.5">
                  Use <code className="bg-gray-100 px-1 rounded">{'{{name}}'}</code> and <code className="bg-gray-100 px-1 rounded">{'{{company}}'}</code> — AI personalises per lead
                </p>
              </div>

              <div className="bg-white border border-border rounded-2xl p-4">
                <div className="flex items-center gap-2 mb-2">
                  <Send className="w-4 h-4 text-amber-500" />
                  <span className="text-sm font-medium">Follow-up</span>
                  <span className="text-xs text-muted-foreground">sent after they accept</span>
                </div>
                <textarea
                  value={followupMessage}
                  onChange={e => setFollowupMessage(e.target.value)}
                  placeholder={FOLLOWUP_PLACEHOLDER}
                  rows={3}
                  className="w-full text-sm border border-border rounded-xl px-3 py-2.5 resize-none focus:outline-none focus:ring-2 focus:ring-primary/30"
                />
              </div>

              <div className="bg-white border border-border rounded-2xl p-4">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-sm font-medium">Daily limit</span>
                  <span className="text-sm font-bold text-primary">{dailyLimit}/day</span>
                </div>
                <input
                  type="range" min={5} max={40} value={dailyLimit}
                  onChange={e => setDailyLimit(Number(e.target.value))}
                  className="w-full"
                />
                <p className="text-xs text-muted-foreground mt-1">Recommended: 15–25/day</p>
              </div>
            </div>

            {launchError && (
              <div className="flex items-start gap-2 bg-red-50 border border-red-200 rounded-xl p-3 text-sm text-red-700 mb-4">
                <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
                <span>{launchError}</span>
              </div>
            )}

            <div className="flex items-center justify-between">
              <button onClick={back} className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
                <ChevronLeft className="w-4 h-4" /> Back
              </button>
              <Button
                onClick={handleLaunch}
                disabled={searchingLeads || connectionNote.length > 300 || launching}
              >
                {launching ? 'Launching...' : 'Launch campaign'}
                <ChevronRight className="w-4 h-4 ml-1" />
              </Button>
            </div>
          </div>
        )}

        {/* ── Step 6: Live dashboard ──────────────────────────────────── */}
        {step === 6 && campaign && stats && (
          <div>
            <div className="flex items-center justify-between mb-6">
              <div>
                <h2 className="text-xl font-bold">{campaign.name}</h2>
                <p className="text-sm text-muted-foreground">{campaign.target_role} · {campaign.daily_limit}/day limit</p>
              </div>
              <Button
                onClick={toggleCampaign}
                disabled={toggling}
                variant={campaign.status === 'running' ? 'outline' : 'primary'}
                className="flex items-center gap-1.5 text-sm"
              >
                {campaign.status === 'running'
                  ? <><Pause className="w-3.5 h-3.5" /> Pause</>
                  : <><Play className="w-3.5 h-3.5" /> Resume</>
                }
              </Button>
            </div>

            {/* Metrics */}
            <div className="grid grid-cols-4 gap-3 mb-6">
              <MetricTile label="Sent" value={stats.total_sent} />
              <MetricTile label="Accepted" value={stats.total_accepted} sub={`${stats.acceptance_rate}%`} />
              <MetricTile label="Follow-ups" value={stats.total_followups_sent} />
              <MetricTile label="Replies" value={stats.total_replied} sub={`${stats.reply_rate}%`} />
            </div>

            {/* Request list */}
            <div className="bg-white border border-border rounded-2xl overflow-hidden">
              <div className="flex border-b border-border">
                {(['all', 'sent', 'accepted', 'replied'] as const).map(tab => (
                  <button
                    key={tab}
                    onClick={() => setActiveTab(tab)}
                    className={`flex-1 py-3 text-xs font-medium transition-colors ${
                      activeTab === tab ? 'text-primary border-b-2 border-primary' : 'text-muted-foreground hover:text-foreground'
                    }`}
                  >
                    {tab.charAt(0).toUpperCase() + tab.slice(1)}
                  </button>
                ))}
              </div>

              {filteredRequests.length === 0 ? (
                <div className="text-center py-10 text-sm text-muted-foreground">
                  {activeTab === 'all' ? 'Requests will appear here as the campaign runs.' : `No ${activeTab} yet.`}
                </div>
              ) : (
                <div className="divide-y divide-border max-h-96 overflow-y-auto">
                  {filteredRequests.map(req => {
                    const s = STATUS_LABELS[req.status] || STATUS_LABELS.pending;
                    return (
                      <div key={req.id} className="px-4 py-3 flex items-center gap-3">
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-1.5">
                            <p className="text-sm font-medium truncate">{req.name}</p>
                            <a href={req.profile_url} target="_blank" rel="noopener noreferrer" className="text-muted-foreground">
                              <ExternalLink className="w-3 h-3" />
                            </a>
                          </div>
                          {req.headline && <p className="text-xs text-muted-foreground truncate">{req.headline}</p>}
                          {req.reply_text && (
                            <p className="text-xs text-foreground/70 italic mt-1 line-clamp-1">"{req.reply_text}"</p>
                          )}
                        </div>
                        <span className={`text-xs px-2 py-0.5 rounded-full font-medium flex-shrink-0 ${s.color}`}>
                          {s.label}
                        </span>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            <p className="text-center text-xs text-muted-foreground mt-4">
              Refreshes every 30s · {campaign.total_leads} total leads loaded
            </p>
          </div>
        )}

      </Container>
    </div>
  );
}
