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
  Loader2, ArrowRight, Sparkles,
} from 'lucide-react';
import api from '@/lib/api';
import type { LinkedInCampaign, CampaignStats, ConnectionRequest } from '@/lib/types/linkedin';

// ── Types ─────────────────────────────────────────────────────────────────────

type Step = 1 | 2 | 3 | 4;

interface QuizData {
  target_role: string;
  target_industries: string[];
  target_locations: string[];
  target_company_sizes: string[];
  target_keywords: string;
  campaign_name: string;
}

interface CandidateSummary {
  id: number;
  primary_role: string;
  target_roles: string[];
  target_industries: string[];
  dream_companies: any[];
  skills: string[];
  location: string | null;
  quiz_complete: boolean;
  created_at: string | null;
}

// ── Constants ─────────────────────────────────────────────────────────────────

const STEPS = ['Profile', 'Connect', 'Messages', 'Live'];

// Where to send students to build their candidate profile (resume upload + AI chat quiz).
// The path is rewritten by ingress; `?return=/lkot` makes the outreach app redirect back here.
const STUDENT_PROFILE_URL = '/outreach/onboarding/upload?return=/lkot';

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

  const STORAGE_KEY = 'li_wizard_v1';

  const [step, setStep] = useState<Step>(1);
  const [restored, setRestored] = useState(false);

  const saveWizard = (s: Step, q: QuizData, cid: number | null) => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ step: s, quiz: q, campaignId: cid }));
    } catch {}
  };

  const clearWizard = () => {
    try { localStorage.removeItem(STORAGE_KEY); } catch {}
  };

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
  const [challengeType, setChallengeType] = useState<'pin' | 'phone_tap'>('pin');
  const [sessionKey, setSessionKey] = useState('');
  const [pin, setPin] = useState('');
  const [pinLoading, setPinLoading] = useState(false);
  const [phoneTapLoading, setPhoneTapLoading] = useState(false);
  const [loginTab, setLoginTab] = useState<'password' | 'cookies' | 'extension'>('password');
  const [liAtCookie, setLiAtCookie] = useState('');
  const [jsessionidCookie, setJsessionidCookie] = useState('');
  const [cookieLoading, setCookieLoading] = useState(false);
  const [extInstalled, setExtInstalled] = useState(false);
  const [extLoading, setExtLoading] = useState(false);

  // Messages state
  const [connectionNote, setConnectionNote] = useState('');
  const [followupMessage, setFollowupMessage] = useState('');
  const [dailyLimit, setDailyLimit] = useState(20);

  // Candidate / profile state (Step 1)
  const [candidate, setCandidate] = useState<CandidateSummary | null>(null);
  const [candidateLoading, setCandidateLoading] = useState(true);
  const [creatingFromProfile, setCreatingFromProfile] = useState(false);
  const [profileError, setProfileError] = useState('');

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
  const [sendingOne, setSendingOne] = useState(false);
  const [sendOneResult, setSendOneResult] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<'all' | 'sent' | 'accepted' | 'replied'>('all');

  const pollRef = useRef<NodeJS.Timeout | null>(null);

  // Step validity
  // Step 1 (Profile): user must have a candidate profile to continue
  // Step 2 (Connect): handled by the connect handlers themselves
  // Step 3 (Messages): always valid (templates pre-filled)
  const canNext = (): boolean => {
    if (step === 1) return !!candidate;
    if (step === 2) return liEmail.length > 0 && liPassword.length > 0;
    return true;
  };

  // Fetch the user's candidate profile on mount
  useEffect(() => {
    let cancelled = false;
    api.get('/linkedin/automation/my-candidate')
      .then(res => { if (!cancelled) setCandidate(res.data?.candidate || null); })
      .catch(() => { if (!cancelled) setCandidate(null); })
      .finally(() => { if (!cancelled) setCandidateLoading(false); });
    return () => { cancelled = true; };
  }, []);

  // Create the LKOT campaign from the candidate profile + advance to Connect step
  const continueWithProfile = async () => {
    if (!candidate) return;
    setCreatingFromProfile(true);
    setProfileError('');
    try {
      const res = await api.post('/linkedin/automation/campaigns/from-profile', {
        candidate_id: candidate.id,
        daily_limit: 5,
      });
      const c = res.data;
      setCampaignId(c.id);
      setCampaign(c);
      // Pre-fill the message templates with the AI-generated note so user can tweak in step 3
      if (c.connection_note) setConnectionNote(c.connection_note);
      if (c.followup_message) setFollowupMessage(c.followup_message);
      // Pre-fill quiz mirror so existing code paths that read quiz.* don't break
      setQuiz(q => ({
        ...q,
        target_role: c.target_role || '',
        target_industries: c.target_industries || [],
        target_locations: c.target_locations || [],
        target_company_sizes: c.target_company_sizes || [],
        target_keywords: c.target_keywords || '',
        campaign_name: c.name || '',
      }));
      const updatedQuiz: QuizData = {
        target_role: c.target_role || '',
        target_industries: c.target_industries || [],
        target_locations: c.target_locations || [],
        target_company_sizes: c.target_company_sizes || [],
        target_keywords: c.target_keywords || '',
        campaign_name: c.name || '',
      };
      saveWizard(2 as Step, updatedQuiz, c.id);
      setStep(2);
    } catch (err: any) {
      setProfileError(err.response?.data?.detail || 'Could not create campaign from profile.');
    } finally {
      setCreatingFromProfile(false);
    }
  };

  const back = () => setStep(s => { const n = Math.max(1, s - 1) as Step; saveWizard(n, quiz, campaignId); return n; });

  // Step 4 → connect + search leads
  const startLeadSearch = async (id: number) => {
    setSearchingLeads(true);
    setLeadsError('');
    setLeads([]);
    await api.post(`/linkedin/automation/campaigns/${id}/search-leads`);
    let attempts = 0;
    const poll = async () => {
      try {
        const [rRes, cRes] = await Promise.all([
          api.get(`/linkedin/automation/campaigns/${id}/requests?limit=50`),
          api.get(`/linkedin/automation/campaigns/${id}`),
        ]);
        if (rRes.data.length > 0) { setLeads(rRes.data); setSearchingLeads(false); return; }
        if (cRes.data.status === 'search_failed') {
          setSearchingLeads(false);
          setLeadsError('search_failed');
          return;
        }
      } catch {}
      attempts++;
      if (attempts < 20) pollRef.current = setTimeout(poll, 3000);
      else { setSearchingLeads(false); setLeadsError('No leads found. Try broadening your search criteria.'); }
    };
    poll();
  };

  const proceedAfterLogin = async () => {
    // If we already have a campaign, check its status.
    // The campaign state may be null if wizard was restored from localStorage without
    // fetching the campaign (e.g. user went back to step 4 from step 5).
    if (campaignId) {
      let cData = campaign;
      if (!cData) {
        try {
          const r = await api.get(`/linkedin/automation/campaigns/${campaignId}`);
          cData = r.data;
          setCampaign(r.data);
        } catch {
          // Campaign no longer exists — fall through to create a new one
        }
      }
      if (cData?.status === 'auth_failed') {
        await api.post(`/linkedin/automation/campaigns/${campaignId}/resume`);
        await fetchDashboard(campaignId);
        setStep(4);
        saveWizard(4, quiz, campaignId);
        pollRef.current = setInterval(() => fetchDashboard(campaignId), 30000);
        return;
      }
    }

    // If the campaign wasn't already created via /from-profile (legacy path), create one now.
    let id = campaignId;
    if (!id) {
      const res = await api.post('/linkedin/automation/campaigns', {
        name: quiz.campaign_name || `${quiz.target_role} outreach`,
        target_role: quiz.target_role,
        target_industries: quiz.target_industries,
        target_locations: quiz.target_locations,
        target_company_sizes: quiz.target_company_sizes,
        target_keywords: quiz.target_keywords || null,
        daily_limit: dailyLimit,
      });
      id = res.data.id;
      setCampaignId(id);
    }
    setStep(3);
    saveWizard(3, quiz, id);
    await startLeadSearch(id!);
  };

  const handleConnect = async () => {
    setConnectLoading(true);
    setConnectError('');
    try {
      const res = await api.post('/linkedin/automation/login', { email: liEmail, password: liPassword });
      if (res.data.challenge_required) {
        setSessionKey(res.data.session_key);
        setChallengeType(res.data.challenge_type === 'phone_tap' ? 'phone_tap' : 'pin');
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

  const handleCheckPhoneTap = async () => {
    setPhoneTapLoading(true);
    setConnectError('');
    try {
      const res = await api.post('/linkedin/automation/login/check-phone-tap', { session_key: sessionKey });
      if (res.data.still_waiting) {
        setConnectError('Not approved yet — tap "Yes" on your phone first, then click Continue.');
        return;
      }
      setChallengeRequired(false);
      await proceedAfterLogin();
    } catch (err: any) {
      setConnectError(err.response?.data?.detail || 'Session expired. Please log in again.');
    } finally {
      setPhoneTapLoading(false);
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

    let timeoutId: ReturnType<typeof setTimeout>;
    const onCookies = (e: Event) => {
      clearTimeout(timeoutId);
      const { li_at, jsessionid, cookies, error } = (e as CustomEvent).detail || {};
      window.removeEventListener('STUDOJO_LI_COOKIES', onCookies);
      if (error || !li_at) {
        setConnectError(error || 'Extension could not read LinkedIn cookies. Make sure you\'re logged in to LinkedIn.');
        setExtLoading(false);
        return;
      }
      api.post('/linkedin/automation/login/cookies', { li_at, jsessionid: jsessionid || '', is_extension: true, cookies })
        .then(() => proceedAfterLogin())
        .catch((err: any) => {
          setConnectError(err.response?.data?.detail || 'LinkedIn session invalid. Please re-login to LinkedIn and try again.');
        })
        .finally(() => setExtLoading(false));
    };

    window.addEventListener('STUDOJO_LI_COOKIES', onCookies);
    window.dispatchEvent(new CustomEvent('STUDOJO_REQUEST_LI_COOKIES'));

    // Timeout if extension doesn't respond
    timeoutId = setTimeout(() => {
      window.removeEventListener('STUDOJO_LI_COOKIES', onCookies);
      setExtLoading(prev => {
        if (prev) setConnectError('Extension did not respond. Try refreshing or installing the extension.');
        return false;
      });
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
      setStep(4);
      saveWizard(4, quiz, campaignId);

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

  const sendOneNow = async () => {
    if (!campaignId) return;
    setSendingOne(true);
    setSendOneResult(null);
    try {
      const res = await api.post(`/linkedin/automation/campaigns/${campaignId}/send-one`);
      setSendOneResult(res.data?.sent ? `Sent to ${res.data.profile_url || 'lead'}` : res.data?.message || 'Done');
      await fetchDashboard(campaignId);
    } catch (e: any) {
      setSendOneResult(e?.response?.data?.detail || 'Failed');
    } finally {
      setSendingOne(false);
    }
  };

  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  // Detect Studojo extension — listen indefinitely so late-installed extensions are caught.
  // The extension's content.js fires STUDOJO_EXT_READY on load AND on STUDOJO_CHECK_EXT.
  useEffect(() => {
    const onReady = () => setExtInstalled(true);
    window.addEventListener('STUDOJO_EXT_READY', onReady);
    // Poke the extension in case it loaded before this effect ran
    window.dispatchEvent(new CustomEvent('STUDOJO_CHECK_EXT'));
    return () => window.removeEventListener('STUDOJO_EXT_READY', onReady);
  }, []);

  // Restore wizard state on mount
  useEffect(() => {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) { setRestored(true); return; }
    try {
      const saved = JSON.parse(raw);
      const savedStep: Step = saved.step ?? 1;
      const savedQuiz: QuizData = saved.quiz ?? quiz;
      const savedCampaignId: number | null = saved.campaignId ?? null;

      setQuiz(savedQuiz);
      setCampaignId(savedCampaignId);

      if (savedStep === 4 && savedCampaignId) {
        // Restore live dashboard
        fetchDashboard(savedCampaignId).then(() => {
          setStep(4);
          setRestored(true);
          pollRef.current = setInterval(() => fetchDashboard(savedCampaignId), 30000);
        }).catch(() => { clearWizard(); setRestored(true); });
      } else if (savedStep === 3 && savedCampaignId) {
        // Restore leads step — re-fetch existing leads (don't re-trigger search)
        api.get(`/linkedin/automation/campaigns/${savedCampaignId}/requests?limit=50`)
          .then(r => {
            if (r.data.length > 0) {
              setLeads(r.data);
            } else {
              setLeadsError('No leads found. Try broadening your search criteria.');
            }
            setStep(3);
            setRestored(true);
          })
          .catch(() => { clearWizard(); setRestored(true); });
      } else {
        setStep(savedStep);
        setRestored(true);
      }
    } catch {
      clearWizard();
      setRestored(true);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
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

        {!restored && (
          <div className="flex items-center justify-center py-20">
            <Spinner className="w-6 h-6 text-primary" />
          </div>
        )}

        {restored && <>

        {/* Progress */}
        <div className="mb-10">
          <ProgressSteps steps={STEPS} currentStep={step} />
        </div>

        {/* ── Step 1: Profile (driven by the outreach quiz) ──────────────── */}
        {step === 1 && (
          <div>
            <h2 className="text-xl font-bold mb-1">Start with your profile</h2>
            <p className="text-sm text-muted-foreground mb-6">
              We use your Studojo profile to find the right people to connect with —
              and write a personal note for each one.
            </p>

            {candidateLoading && (
              <div className="flex items-center gap-2 text-sm text-muted-foreground py-8">
                <Loader2 className="w-4 h-4 animate-spin" /> Checking your profile…
              </div>
            )}

            {/* No candidate profile yet — send them to upload + quiz */}
            {!candidateLoading && !candidate && (
              <div className="rounded-2xl border border-border p-6">
                <p className="text-sm font-medium mb-1">You haven't built your profile yet</p>
                <p className="text-sm text-muted-foreground mb-5">
                  Upload your resume and answer a few quick questions. Takes about 3 minutes —
                  then we'll find LinkedIn connections matched to your goals.
                </p>
                <a
                  href={STUDENT_PROFILE_URL}
                  className="inline-flex items-center gap-2 px-4 py-2.5 bg-primary text-white text-sm font-medium rounded-xl hover:bg-primary/90 transition-colors"
                >
                  Build my profile <ArrowRight className="w-4 h-4" />
                </a>
              </div>
            )}

            {/* Candidate exists but quiz not finished */}
            {!candidateLoading && candidate && !candidate.quiz_complete && (
              <div className="rounded-2xl border border-amber-300 bg-amber-50 p-6">
                <p className="text-sm font-medium mb-1">Almost there — finish your quiz</p>
                <p className="text-sm text-muted-foreground mb-5">
                  Your resume is in, but we need a few quiz answers to know what roles
                  and industries to target.
                </p>
                <a
                  href="/outreach/onboarding/chat?return=/lkot"
                  className="inline-flex items-center gap-2 px-4 py-2.5 bg-primary text-white text-sm font-medium rounded-xl hover:bg-primary/90 transition-colors"
                >
                  Finish the quiz <ArrowRight className="w-4 h-4" />
                </a>
              </div>
            )}

            {/* Ready — show profile summary + continue */}
            {!candidateLoading && candidate && candidate.quiz_complete && (
              <div>
                <div className="rounded-2xl border border-border p-5 space-y-4">
                  <div>
                    <p className="text-xs uppercase tracking-wide text-muted-foreground mb-1">Target role</p>
                    <p className="text-sm font-medium">{candidate.primary_role || '—'}</p>
                  </div>
                  {candidate.target_industries?.length > 0 && (
                    <div>
                      <p className="text-xs uppercase tracking-wide text-muted-foreground mb-1.5">Industries</p>
                      <div className="flex flex-wrap gap-1.5">
                        {candidate.target_industries.map((ind: any, i: number) => (
                          <span key={i} className="px-2.5 py-1 bg-primary/5 border border-primary/20 rounded-lg text-xs">
                            {typeof ind === 'string' ? ind : ind.industry || ind.name}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                  {candidate.location && (
                    <div>
                      <p className="text-xs uppercase tracking-wide text-muted-foreground mb-1">Location</p>
                      <p className="text-sm">{candidate.location}</p>
                    </div>
                  )}
                  {candidate.skills?.length > 0 && (
                    <div>
                      <p className="text-xs uppercase tracking-wide text-muted-foreground mb-1.5">Skills</p>
                      <div className="flex flex-wrap gap-1.5">
                        {candidate.skills.slice(0, 8).map((s: string, i: number) => (
                          <span key={i} className="px-2.5 py-1 bg-muted rounded-lg text-xs">{s}</span>
                        ))}
                      </div>
                    </div>
                  )}
                </div>

                <p className="text-xs text-muted-foreground mt-3">
                  Want to change this?{' '}
                  <a href="/outreach/onboarding/chat?return=/lkot" className="text-primary underline">
                    Redo the quiz
                  </a>
                </p>

                {profileError && (
                  <p className="text-sm text-red-600 mt-3">{profileError}</p>
                )}

                <button
                  onClick={continueWithProfile}
                  disabled={creatingFromProfile}
                  className="mt-6 w-full inline-flex items-center justify-center gap-2 px-4 py-3 bg-primary text-white text-sm font-medium rounded-xl hover:bg-primary/90 transition-colors disabled:opacity-60"
                >
                  {creatingFromProfile
                    ? <><Loader2 className="w-4 h-4 animate-spin" /> Setting up your campaign…</>
                    : <>Continue with this profile <ArrowRight className="w-4 h-4" /></>}
                </button>
              </div>
            )}
          </div>
        )}

        {/* ── Step 2: Connect LinkedIn ────────────────────────────────── */}
        {step === 2 && (
          <div>
            <div className="flex items-center gap-3 mb-6">
              <div className="w-10 h-10 bg-[#0077B5] rounded-xl flex items-center justify-center">
                <Linkedin className="w-5 h-5 text-white" />
              </div>
              <div>
                <h2 className="text-xl font-bold">
                  {!challengeRequired ? 'Connect your LinkedIn' : challengeType === 'phone_tap' ? 'Check your phone' : 'Check your email'}
                </h2>
                <p className="text-sm text-muted-foreground">
                  {!challengeRequired
                    ? "We'll search for leads and send requests on your behalf."
                    : challengeType === 'phone_tap'
                    ? 'LinkedIn sent a notification to your phone'
                    : `LinkedIn sent a verification code to ${liEmail}`}
                </p>
              </div>
            </div>

            {!challengeRequired ? (
              <>
                {/* ── Tab selector ───────────────────────────────────────── */}
                <div className="flex gap-1 bg-gray-100 rounded-xl p-1 mb-5">
                  {(['password', 'cookies', 'extension'] as const).map(tab => (
                    <button
                      key={tab}
                      onClick={() => { setLoginTab(tab); setConnectError(''); }}
                      className={`flex-1 py-1.5 text-xs font-medium rounded-lg transition-all ${
                        loginTab === tab
                          ? 'bg-white shadow-sm text-foreground'
                          : 'text-muted-foreground hover:text-foreground'
                      }`}
                    >
                      {tab === 'password' ? 'Email & Password' : tab === 'cookies' ? 'Paste Cookies' : 'Extension'}
                    </button>
                  ))}
                </div>

                {/* ── Email & Password tab ────────────────────────────────── */}
                {loginTab === 'password' && (
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
                    </div>
                    <div className="space-y-2 mb-4">
                      {['Credentials encrypted with AES-256 before storage', 'Only used to send connection requests you configure', 'Disconnect at any time'].map(item => (
                        <div key={item} className="flex items-center gap-2.5 text-sm text-muted-foreground">
                          <ShieldCheck className="w-4 h-4 text-green-600 flex-shrink-0" /><span>{item}</span>
                        </div>
                      ))}
                    </div>
                    <div className="bg-amber-50 border border-amber-200 rounded-xl p-3 mb-5 text-xs text-amber-800">
                      <p className="font-semibold mb-0.5">Heads up — LinkedIn may verify your identity</p>
                      <p>After clicking Connect, a notification may pop up on your phone from the LinkedIn app. Tap <strong>&quot;Yes, it&apos;s me&quot;</strong> to approve the login.</p>
                    </div>
                  </>
                )}

                {/* ── Cookies tab ─────────────────────────────────────────── */}
                {loginTab === 'cookies' && (
                  <div className="space-y-3 mb-5">
                    <div className="bg-blue-50 border border-blue-200 rounded-xl p-3 text-xs text-blue-700 space-y-1">
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
                    </div>
                  </div>
                )}

                {/* ── Extension tab ───────────────────────────────────────── */}
                {loginTab === 'extension' && (
                  <div className="mb-5">
                    {extInstalled ? (
                      <div className="bg-white border border-border rounded-2xl p-5 space-y-3">
                        <div className="flex items-center gap-3">
                          <div className="w-8 h-8 bg-green-100 rounded-full flex items-center justify-center flex-shrink-0">
                            <CheckCircle className="w-4 h-4 text-green-600" />
                          </div>
                          <div>
                            <p className="text-sm font-medium">Extension detected</p>
                            <p className="text-xs text-muted-foreground">We&apos;ll read your LinkedIn cookies automatically — no copy-paste needed.</p>
                          </div>
                        </div>
                        <div className="space-y-1.5">
                          {['Reads cookies directly from your browser', 'No password ever sent to our servers', 'One click — works as long as you\'re logged in to LinkedIn'].map(item => (
                            <div key={item} className="flex items-center gap-2.5 text-sm text-muted-foreground">
                              <ShieldCheck className="w-4 h-4 text-green-600 flex-shrink-0" /><span>{item}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    ) : (
                      <div className="bg-white border border-border rounded-2xl p-5 space-y-4">
                        <p className="text-sm font-semibold">Install the Studojo extension (2 min)</p>
                        <ol className="space-y-2 text-sm text-muted-foreground">
                          <li>1. Download and unzip the extension below</li>
                          <li>2. Open <strong>chrome://extensions</strong> → enable <strong>Developer mode</strong></li>
                          <li>3. Click <strong>Load unpacked</strong> → select the unzipped folder</li>
                          <li>4. Come back here — the tab will update automatically</li>
                        </ol>
                        <a
                          href="/lkot/extension/studojo-extension.zip"
                          download
                          className="inline-flex items-center gap-2 px-4 py-2 bg-primary text-white text-sm font-medium rounded-xl hover:bg-primary/90 transition-colors"
                        >
                          <ExternalLink className="w-3.5 h-3.5" /> Download extension
                        </a>
                        <p className="text-xs text-muted-foreground">After installing, click the Extension tab again — no page reload needed.</p>
                      </div>
                    )}
                  </div>
                )}

                {/* ── Shared error ────────────────────────────────────────── */}
                {connectError && (
                  <div className="flex items-start gap-2 bg-red-50 border border-red-200 rounded-xl p-3 text-sm text-red-700 mb-4">
                    <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" /><span>{connectError}</span>
                  </div>
                )}

                {/* ── Footer nav ──────────────────────────────────────────── */}
                <div className="flex items-center justify-between mt-2">
                  <button onClick={back} className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors">
                    <ChevronLeft className="w-4 h-4" /> Back
                  </button>
                  {loginTab === 'password' && (
                    <Button onClick={handleConnect} disabled={!canNext() || connectLoading}>
                      {connectLoading ? 'Connecting...' : 'Connect & find leads'}<ChevronRight className="w-4 h-4 ml-1" />
                    </Button>
                  )}
                  {loginTab === 'cookies' && (
                    <Button onClick={handleCookieLogin} disabled={!liAtCookie.trim() || !jsessionidCookie.trim() || cookieLoading}>
                      {cookieLoading ? 'Connecting...' : 'Connect & find leads'}<ChevronRight className="w-4 h-4 ml-1" />
                    </Button>
                  )}
                  {loginTab === 'extension' && (
                    <Button onClick={handleExtensionLogin} disabled={extLoading || !extInstalled}>
                      {extLoading ? 'Reading cookies...' : extInstalled ? 'Connect & find leads' : 'Install extension first'}
                      <ChevronRight className="w-4 h-4 ml-1" />
                    </Button>
                  )}
                </div>
              </>
            ) : challengeType === 'phone_tap' ? (
              <>
                <div className="bg-white border border-border rounded-2xl p-5 space-y-4 mb-5">
                  <div className="text-center py-3 space-y-3">
                    <div className="w-14 h-14 bg-[#0077B5]/10 rounded-full flex items-center justify-center mx-auto">
                      <Linkedin className="w-7 h-7 text-[#0077B5]" />
                    </div>
                    <div>
                      <p className="text-sm font-medium text-foreground">Open your LinkedIn app</p>
                      <p className="text-sm text-muted-foreground mt-1">
                        Tap <strong>&quot;Yes, it&apos;s me&quot;</strong> on the notification, then click Continue below.
                      </p>
                    </div>
                    <div className="bg-blue-50 border border-blue-200 rounded-xl p-3 text-xs text-blue-700 text-left space-y-1">
                      <p>1. Check your phone for a LinkedIn notification</p>
                      <p>2. Tap <strong>Yes, it&apos;s me</strong> to approve the sign-in</p>
                      <p>3. Come back here and click <strong>Continue</strong></p>
                    </div>
                  </div>
                  {connectError && (
                    <div className="flex items-start gap-2 bg-red-50 border border-red-200 rounded-xl p-3 text-sm text-red-700">
                      <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" /><span>{connectError}</span>
                    </div>
                  )}
                </div>
                <div className="flex items-center justify-between">
                  <button onClick={() => { setChallengeRequired(false); setConnectError(''); }}
                    className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
                    <ChevronLeft className="w-4 h-4" /> Back
                  </button>
                  <Button onClick={handleCheckPhoneTap} disabled={phoneTapLoading}>
                    {phoneTapLoading ? 'Checking...' : 'I approved it — Continue'}<ChevronRight className="w-4 h-4 ml-1" />
                  </Button>
                </div>
              </>
            ) : (
              <>
                <div className="bg-white border border-border rounded-2xl p-5 space-y-4 mb-5">
                  <div>
                    <label className="text-sm font-medium block mb-1.5">Verification code</label>
                    <input
                      type="text" inputMode="numeric" value={pin}
                      onChange={e => setPin(e.target.value.replace(/\D/g, '').slice(0, 8))}
                      placeholder="Enter the code LinkedIn emailed you"
                      className="w-full border border-border rounded-xl px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30 tracking-widest text-center text-lg font-mono"
                      autoFocus onKeyDown={e => e.key === 'Enter' && pin.length >= 4 && handleVerifyPin()} />
                  </div>
                  {connectError && (
                    <div className="flex items-start gap-2 bg-red-50 border border-red-200 rounded-xl p-3 text-sm text-red-700">
                      <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" /><span>{connectError}</span>
                    </div>
                  )}
                </div>
                <div className="flex items-center justify-between">
                  <button onClick={() => { setChallengeRequired(false); setPin(''); setConnectError(''); }}
                    className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
                    <ChevronLeft className="w-4 h-4" /> Back
                  </button>
                  <Button onClick={handleVerifyPin} disabled={pin.length < 4 || pinLoading}>
                    {pinLoading ? 'Verifying...' : 'Verify & continue'}<ChevronRight className="w-4 h-4 ml-1" />
                  </Button>
                </div>
                <div className="mt-4 pt-4 border-t border-border text-center">
                  <p className="text-xs text-muted-foreground mb-2">Code not arriving?</p>
                  <button onClick={() => { setChallengeRequired(false); setPin(''); setConnectError(''); }}
                    className="text-sm text-primary hover:underline font-medium">
                    Try again with a different email
                  </button>
                </div>
              </>
            )}
          </div>
        )}

        {/* ── Step 3: Leads + messages ────────────────────────────────── */}
        {step === 3 && (
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
              ) : leadsError === 'search_failed' ? (
                <div className="bg-amber-50 border border-amber-200 rounded-2xl p-5 space-y-3">
                  <div className="flex items-start gap-2 text-amber-800">
                    <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
                    <div>
                      <p className="text-sm font-medium">No leads found</p>
                      <p className="text-xs mt-0.5 text-amber-700">
                        This usually means your LinkedIn session expired or the search criteria returned no results.
                      </p>
                    </div>
                  </div>
                  <div className="flex flex-col gap-2">
                    <Button
                      onClick={() => campaignId && startLeadSearch(campaignId)}
                      className="w-full text-sm"
                    >
                      <Search className="w-3.5 h-3.5 mr-1.5" /> Retry search
                    </Button>
                    <button
                      onClick={() => {
                        setLeadsError('');
                        setStep(2);
                        saveWizard(2, quiz, campaignId);
                      }}
                      className="text-xs text-amber-700 hover:underline text-center"
                    >
                      Reconnect LinkedIn session instead
                    </button>
                  </div>
                </div>
              ) : leadsError ? (
                <div className="bg-red-50 border border-red-200 rounded-xl p-4 space-y-3">
                  <p className="text-sm text-red-700">{leadsError}</p>
                  <Button
                    onClick={() => campaignId && startLeadSearch(campaignId)}
                    className="w-full text-sm"
                  >
                    <Search className="w-3.5 h-3.5 mr-1.5" /> Retry search
                  </Button>
                </div>
              ) : (
                <div className="bg-white border border-border rounded-2xl overflow-hidden">
                  <div className="px-4 py-3 border-b border-border flex items-center gap-2">
                    <Users className="w-4 h-4 text-primary" />
                    <span className="text-sm font-medium">{leads.length} leads found</span>
                  </div>
                  <div className="divide-y divide-border max-h-48 overflow-y-auto">
                    {leads.slice(0, 10).map(lead => (
                      <div key={lead.id} className="px-4 py-2.5 flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <p className="text-sm font-medium truncate">{lead.name}</p>
                          {lead.headline && <p className="text-xs text-muted-foreground truncate">{lead.headline}</p>}
                          {lead.match_reason && (
                            <p className="text-xs text-primary/80 mt-0.5 line-clamp-2 flex items-start gap-1">
                              <Sparkles className="w-3 h-3 mt-0.5 flex-shrink-0" />
                              <span>{lead.match_reason}</span>
                            </p>
                          )}
                        </div>
                        <a href={lead.profile_url} target="_blank" rel="noopener noreferrer" className="text-muted-foreground hover:text-foreground flex-shrink-0 mt-0.5">
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

        {/* ── Step 4: Live dashboard ──────────────────────────────────── */}
        {step === 4 && campaign && stats && (
          <div>
            <div className="flex items-center justify-between mb-6">
              <div>
                <h2 className="text-xl font-bold">{campaign.name}</h2>
                <p className="text-sm text-muted-foreground">{campaign.target_role} · {campaign.daily_limit}/day limit</p>
              </div>
              <div className="flex items-center gap-2">
                <Button
                  onClick={sendOneNow}
                  disabled={sendingOne || campaign.status !== 'running'}
                  variant="outline"
                  className="flex items-center gap-1.5 text-sm"
                  title="Send one connection request immediately (bypasses IST window)"
                >
                  <Send className="w-3.5 h-3.5" />
                  {sendingOne ? 'Sending…' : 'Send one now'}
                </Button>
                <Button
                  onClick={toggleCampaign}
                  disabled={toggling || campaign.status === 'auth_failed'}
                  variant={campaign.status === 'running' ? 'outline' : 'primary'}
                  className="flex items-center gap-1.5 text-sm"
                >
                  {campaign.status === 'running'
                    ? <><Pause className="w-3.5 h-3.5" /> Pause</>
                    : <><Play className="w-3.5 h-3.5" /> Resume</>
                  }
                </Button>
              </div>
            </div>
            {sendOneResult && (
              <div className={`text-xs px-3 py-2 rounded-xl mb-4 ${sendOneResult.startsWith('Sent') ? 'bg-green-50 text-green-700 border border-green-200' : 'bg-red-50 text-red-700 border border-red-200'}`}>
                {sendOneResult}
              </div>
            )}

            {/* Auth failed banner */}
            {campaign.status === 'auth_failed' && (
              <div className="bg-amber-50 border border-amber-200 rounded-2xl p-4 mb-5 flex items-start gap-3">
                <AlertCircle className="w-4 h-4 text-amber-600 mt-0.5 flex-shrink-0" />
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-amber-800">LinkedIn session expired</p>
                  <p className="text-xs text-amber-700 mt-0.5">Campaign paused — reconnect to resume sending.</p>
                </div>
                <button
                  onClick={() => {
                    setStep(2);
                    saveWizard(2, quiz, campaignId);
                  }}
                  className="flex-shrink-0 text-xs font-medium text-amber-800 underline hover:no-underline"
                >
                  Reconnect
                </button>
              </div>
            )}

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
                          {req.match_reason && (
                            <p className="text-xs text-primary/80 mt-1 line-clamp-2 flex items-start gap-1">
                              <Sparkles className="w-3 h-3 mt-0.5 flex-shrink-0" />
                              <span>{req.match_reason}</span>
                            </p>
                          )}
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

        </> }

      </Container>
    </div>
  );
}
