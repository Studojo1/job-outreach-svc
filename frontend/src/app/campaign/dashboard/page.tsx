'use client';

import { useEffect, useState, useCallback, useRef } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { useAppStore } from '@/store/useAppStore';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { MetricCard } from '@/components/features/MetricCard';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Spinner } from '@/components/ui/Spinner';
import { Send, AlertCircle, BarChart3, Pause, Play, Users, CheckCircle, XCircle, Clock, FlaskConical, Mail, Linkedin } from 'lucide-react';
import api from '@/lib/api';
import { formatTimestamp } from '@/lib/formatTime';
import type { CampaignMetrics } from '@/lib/types/campaign';

const LI_STATUS: Record<string, { label: string; className: string }> = {
  pending:       { label: 'Queued',       className: 'bg-gray-100 text-gray-500' },
  in_progress:   { label: 'Sending…',     className: 'bg-amber-100 text-amber-700' },
  sent:          { label: 'Sent',         className: 'bg-blue-100 text-blue-700' },
  accepted:      { label: 'Accepted',     className: 'bg-green-100 text-green-700' },
  followup_sent: { label: 'Follow-up',    className: 'bg-violet-100 text-violet-700' },
  replied:       { label: 'Replied',      className: 'bg-emerald-100 text-emerald-700' },
  error:         { label: 'Error',        className: 'bg-red-100 text-red-600' },
};

interface TestLead {
  lead_name: string;
  company: string;
  email: string;
  status: string;
  subject: string;
  schedule_offset: number;
  error?: string;
}

interface TestJobData {
  job_id: string;
  status: string;
  started_at: string;
  total: number;
  emails_sent: number;
  emails_failed: number;
  leads: TestLead[];
  error?: string;
}

interface CampaignEmail {
  id: number;
  lead_name: string;
  lead_company: string;
  to_email: string;
  subject: string;
  status: string;
  scheduled_at: string | null;
  sent_at: string | null;
}

function CountdownCell({ startedAt, offsetSeconds, status }: { startedAt: string; offsetSeconds: number; status: string }) {
  const [remaining, setRemaining] = useState<number | null>(null);

  useEffect(() => {
    if (status === 'sent' || status === 'failed') { setRemaining(null); return; }
    const start = new Date(startedAt).getTime();
    const targetTime = start + offsetSeconds * 1000;

    const tick = () => {
      const diff = Math.max(0, Math.ceil((targetTime - Date.now()) / 1000));
      setRemaining(diff);
    };
    tick();
    const interval = setInterval(tick, 1000);
    return () => clearInterval(interval);
  }, [startedAt, offsetSeconds, status]);

  if (status === 'sent' || status === 'failed') return <span className="text-muted">-</span>;
  if (status === 'sending') return <span className="text-amber-600 font-bold">Sending now...</span>;
  if (remaining === null) return <span className="text-muted">-</span>;
  if (remaining <= 0) return <span className="text-amber-600 font-bold">Sending now...</span>;
  return <span className="text-primary font-bold">Sending in {remaining}s</span>;
}

function StatusBadge({ status }: { status: string }) {
  if (status === 'sent') return (
    <div className="flex items-center gap-1">
      <CheckCircle className="w-4 h-4 text-secondary" />
      <span className="text-secondary font-bold">Sent</span>
    </div>
  );
  if (status === 'failed') return (
    <div className="flex items-center gap-1">
      <XCircle className="w-4 h-4 text-error" />
      <span className="text-error font-bold">Failed</span>
    </div>
  );
  if (status === 'sending') return (
    <div className="flex items-center gap-1">
      <Spinner size="sm" />
      <span className="text-amber-600 font-bold">Sending</span>
    </div>
  );
  return (
    <div className="flex items-center gap-1">
      <Clock className="w-4 h-4 text-muted" />
      <span className="text-muted font-bold">To Send</span>
    </div>
  );
}

export default function DashboardPage() {
  const router = useRouter();
  const { loading: authLoading } = useAuth();
  const { campaignId, setCampaignId, user, linkedInCampaignId, planType } = useAppStore();
  const tz = user?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone;

  // Test mode state
  const [testJobId, setTestJobId] = useState<string | null>(null);
  const [testJob, setTestJob] = useState<TestJobData | null>(null);
  const [testStartedAt, setTestStartedAt] = useState('');

  // Campaign mode state
  const [metrics, setMetrics] = useState<CampaignMetrics | null>(null);
  const [emails, setEmails] = useState<CampaignEmail[]>([]);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const pollRef = useRef<NodeJS.Timeout | null>(null);

  // LinkedIn tab state
  const [dashTab, setDashTab] = useState<'email' | 'linkedin'>('email');
  const [liStats, setLiStats] = useState<any>(null);
  const [liRequests, setLiRequests] = useState<any[]>([]);
  const [liCampaign, setLiCampaign] = useState<any>(null);
  const [liToggling, setLiToggling] = useState(false);
  const liPollRef = useRef<NodeJS.Timeout | null>(null);

  // On mount: check for test job or campaign, recover campaignId if missing
  useEffect(() => {
    const storedJobId = sessionStorage.getItem('test_job_id');
    const storedStartedAt = sessionStorage.getItem('test_started_at');
    if (storedJobId) {
      setTestJobId(storedJobId);
      setTestStartedAt(storedStartedAt || new Date().toISOString());
      setLoading(false);
      return;
    }

    // If no campaignId in store, try to recover from backend
    if (!campaignId) {
      api.get('/campaign/user/latest')
        .then((res) => {
          const c = res.data?.campaign;
          if (c?.id) {
            setCampaignId(c.id);
          }
        })
        .catch(() => { /* no campaign found */ })
        .finally(() => setLoading(false));
    } else {
      setLoading(false);
    }
  }, []);

  // Poll test launch status
  const pollTestStatus = useCallback(async () => {
    if (!testJobId) return;
    try {
      const res = await api.get(`/campaign/test-launch/${testJobId}/status`);
      const data = res.data as TestJobData;
      setTestJob(data);
      if (data.started_at) setTestStartedAt(data.started_at);
      if (data.status === 'completed' || data.status === 'failed') {
        if (pollRef.current) clearInterval(pollRef.current);
        sessionStorage.removeItem('test_job_id');
        sessionStorage.removeItem('test_started_at');
      }
    } catch { /* network blip — keep polling */ }
  }, [testJobId]);

  useEffect(() => {
    if (!testJobId) return;
    pollTestStatus();
    pollRef.current = setInterval(pollTestStatus, 3000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [testJobId, pollTestStatus]);

  // Campaign metrics polling
  const fetchCampaignData = useCallback(async () => {
    if (!campaignId || testJobId) return;
    try {
      const [metricsRes, emailsRes] = await Promise.all([
        api.get(`/campaign/${campaignId}/metrics`),
        api.get(`/campaign/${campaignId}/emails`),
      ]);
      setMetrics(metricsRes.data);
      setEmails(emailsRes.data.emails || []);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to load campaign data');
    }
  }, [campaignId, testJobId]);

  useEffect(() => {
    if (!campaignId || testJobId) return;
    fetchCampaignData();
    const interval = setInterval(fetchCampaignData, 10000);
    return () => clearInterval(interval);
  }, [campaignId, testJobId, fetchCampaignData]);

  const fetchLinkedInData = useCallback(async () => {
    if (!linkedInCampaignId) return;
    try {
      const [cRes, sRes, rRes] = await Promise.all([
        api.get(`/linkedin/automation/campaigns/${linkedInCampaignId}`),
        api.get(`/linkedin/automation/campaigns/${linkedInCampaignId}/stats`),
        api.get(`/linkedin/automation/campaigns/${linkedInCampaignId}/requests?limit=200`),
      ]);
      setLiCampaign(cRes.data);
      setLiStats(sRes.data);
      setLiRequests(rRes.data);
    } catch { /* non-fatal */ }
  }, [linkedInCampaignId]);

  useEffect(() => {
    if (!linkedInCampaignId) return;
    fetchLinkedInData();
    liPollRef.current = setInterval(fetchLinkedInData, 30000);
    return () => { if (liPollRef.current) clearInterval(liPollRef.current); };
  }, [linkedInCampaignId, fetchLinkedInData]);

  const handleTransition = async (status: string) => {
    if (!campaignId) return;
    try {
      await api.post(`/campaign/${campaignId}/transition`, { target_status: status });
      fetchCampaignData();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to update campaign');
    }
  };

  if (authLoading || loading) return <div className="min-h-screen flex items-center justify-center bg-white"><Spinner /></div>;

  // ─── Test Mode Dashboard ─────────────────────────────────────────────
  if (testJobId) {
    const leads = testJob?.leads || [];
    const total = testJob?.total || leads.length || 0;
    const sent = testJob?.emails_sent || 0;
    const failed = testJob?.emails_failed || 0;
    const toSend = Math.max(0, total - sent - failed);
    const isComplete = testJob?.status === 'completed';
    const isFailed = testJob?.status === 'failed' && testJob?.error;

    return (
      <div className="min-h-screen bg-white">
        <Navbar />
        <Container className="py-8">
          <div className="space-y-8 animate-fade-in">
            <div className="flex items-center justify-between">
              <div>
                <h1 className="font-clash text-2xl font-bold">Campaign Dashboard</h1>
                <Badge variant={isComplete ? 'success' : 'warning'} className="mt-2">
                  <FlaskConical className="w-3 h-3 mr-1 inline" />
                  {isComplete ? 'Test Complete' : isFailed ? 'Test Failed' : 'Test In Progress'}
                </Badge>
              </div>
              <Button onClick={() => { sessionStorage.removeItem('test_job_id'); sessionStorage.removeItem('test_started_at'); router.push('/campaign/setup'); }}>
                Back to Setup
              </Button>
            </div>

            {isFailed && (
              <div className="rounded-2xl border-2 border-red-300 bg-red-50 p-6">
                <p className="text-error font-satoshi font-bold">{testJob?.error}</p>
              </div>
            )}

            {/* Summary Stats */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <MetricCard label="To Send" value={toSend} icon={<Clock className="w-5 h-5" />} />
              <MetricCard label="Sent" value={sent} icon={<Send className="w-5 h-5" />} trend={sent > 0 ? 'up' : undefined} />
              <MetricCard label="Failed" value={failed} icon={<AlertCircle className="w-5 h-5" />} />
            </div>

            {/* Progress Bar */}
            {total > 0 && (
              <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-6">
                <div className="flex items-center gap-4 mb-4">
                  <div className="w-10 h-10 rounded-xl bg-brand-purple-bg border-2 border-ink flex items-center justify-center text-primary"><BarChart3 className="w-5 h-5" /></div>
                  <h3 className="font-clash text-lg font-bold">Sending Progress</h3>
                  {!isComplete && !isFailed && <Spinner size="sm" />}
                </div>
                <div className="w-full h-3 bg-surface-muted rounded-full overflow-hidden border-2 border-ink/10">
                  <div className="h-full flex">
                    <div className="bg-secondary transition-all duration-500" style={{ width: `${total > 0 ? (sent / total) * 100 : 0}%` }} />
                    <div className="bg-error transition-all duration-500" style={{ width: `${total > 0 ? (failed / total) * 100 : 0}%` }} />
                  </div>
                </div>
                <p className="text-sm text-muted mt-2 font-satoshi">{sent + failed} of {total} emails processed</p>
              </div>
            )}

            {/* Leads Table */}
            <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-6">
              <div className="flex items-center gap-4 mb-4">
                <div className="w-10 h-10 rounded-xl bg-studojo-green-bg border-2 border-ink flex items-center justify-center text-secondary"><Users className="w-5 h-5" /></div>
                <h3 className="font-clash text-lg font-bold">Campaign Leads</h3>
                <Badge variant="primary">{total} leads</Badge>
              </div>
              {leads.length === 0 ? (
                <div className="flex justify-center py-8"><Spinner /><span className="ml-3 text-muted font-satoshi">Loading leads...</span></div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm font-satoshi">
                    <thead>
                      <tr className="border-b-2 border-ink/10">
                        <th className="text-left py-3 px-2 text-xs font-bold text-muted uppercase">Lead Name</th>
                        <th className="text-left py-3 px-2 text-xs font-bold text-muted uppercase">Email</th>
                        <th className="text-left py-3 px-2 text-xs font-bold text-muted uppercase hidden md:table-cell">Company</th>
                        <th className="text-left py-3 px-2 text-xs font-bold text-muted uppercase">Status</th>
                        <th className="text-left py-3 px-2 text-xs font-bold text-muted uppercase">Schedule</th>
                      </tr>
                    </thead>
                    <tbody>
                      {leads.map((lead, i) => (
                        <tr key={i} className="border-b border-ink/5 hover:bg-surface-muted transition-colors">
                          <td className="py-3 px-2 font-bold">{lead.lead_name}</td>
                          <td className="py-3 px-2 text-muted truncate max-w-[200px]">{lead.email}</td>
                          <td className="py-3 px-2 text-muted hidden md:table-cell">{lead.company}</td>
                          <td className="py-3 px-2"><StatusBadge status={lead.status} /></td>
                          <td className="py-3 px-2 text-sm">
                            <CountdownCell startedAt={testStartedAt} offsetSeconds={lead.schedule_offset} status={lead.status} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>
        </Container>
      </div>
    );
  }

  // ─── Campaign Mode Dashboard ─────────────────────────────────────────
  if (!campaignId) {
    return (
      <div className="min-h-screen bg-white">
        <Navbar />
        <Container className="max-w-onboarding py-8 text-center">
          <p className="text-base text-muted mt-10 font-satoshi">No active campaign.</p>
          <Button onClick={() => router.push('/campaign/setup')} className="mt-6">Create Campaign</Button>
        </Container>
      </div>
    );
  }

  const statusColor: Record<string, 'primary' | 'success' | 'warning' | 'default'> = {
    draft: 'default', running: 'success', paused: 'warning', completed: 'primary',
  };

  const campaignTotal = metrics?.emails_total || 0;
  const campaignSent = metrics?.emails_sent || 0;
  const campaignFailed = metrics?.emails_failed || 0;
  const campaignToSend = metrics?.emails_queued || 0;

  return (
    <div className="min-h-screen bg-white">
      <Navbar />
      <Container className="py-8">
        {/* Channel tab switcher — only shown when user has both channels */}
        {planType === 'both' && (
          <div className="flex gap-2 mb-6 rounded-2xl border-2 border-ink bg-surface-muted p-1">
            <button
              onClick={() => setDashTab('email')}
              className={`flex-1 flex items-center justify-center gap-2 py-2.5 rounded-xl text-sm font-bold font-satoshi transition-all ${dashTab === 'email' ? 'bg-white border-2 border-ink shadow-brutal text-primary' : 'text-muted hover:text-ink'}`}
            >
              <Mail className="w-4 h-4" /> Email
            </button>
            <button
              onClick={() => setDashTab('linkedin')}
              className={`flex-1 flex items-center justify-center gap-2 py-2.5 rounded-xl text-sm font-bold font-satoshi transition-all ${dashTab === 'linkedin' ? 'bg-white border-2 border-ink shadow-brutal text-primary' : 'text-muted hover:text-ink'}`}
            >
              <Linkedin className="w-4 h-4" /> LinkedIn
            </button>
          </div>
        )}

        {/* LinkedIn tab */}
        {(planType === 'linkedin' || (planType === 'both' && dashTab === 'linkedin')) && (
          <LinkedInDashboard
            campaign={liCampaign}
            stats={liStats}
            requests={liRequests}
            toggling={liToggling}
            onToggle={async () => {
              if (!liCampaign || !linkedInCampaignId) return;
              setLiToggling(true);
              try {
                const action = liCampaign.status === 'running' ? 'pause' : 'resume';
                await api.post(`/linkedin/automation/campaigns/${linkedInCampaignId}/${action}`);
                await fetchLinkedInData();
              } finally { setLiToggling(false); }
            }}
          />
        )}

        {/* Email tab (hidden when LinkedIn-only or viewing LinkedIn tab on both-plan) */}
        {planType !== 'linkedin' && (planType !== 'both' || dashTab === 'email') && (
        <>
        {error ? (
          <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-8 text-center">
            <p className="text-error font-satoshi">{error}</p>
          </div>
        ) : metrics ? (
          <div className="space-y-8 animate-fade-in">
            <div className="flex items-center justify-between">
              <div>
                <h1 className="font-clash text-2xl font-bold">{metrics.campaign_name}</h1>
                <Badge variant={statusColor[metrics.status] || 'default'} className="mt-2">
                  {metrics.status.charAt(0).toUpperCase() + metrics.status.slice(1)}
                </Badge>
              </div>
              <div className="flex gap-3">
                {metrics.status === 'running' && (
                  <Button variant="outline" onClick={() => handleTransition('paused')}>
                    <Pause className="w-4 h-4 mr-2 inline" /> Pause
                  </Button>
                )}
                {metrics.status === 'paused' && (
                  <Button onClick={() => handleTransition('running')}>
                    <Play className="w-4 h-4 mr-2 inline" /> Resume
                  </Button>
                )}
              </div>
            </div>

            {/* Summary Stats */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <MetricCard label="To Send" value={campaignToSend} icon={<Clock className="w-5 h-5" />} />
              <MetricCard label="Sent" value={campaignSent} icon={<Send className="w-5 h-5" />} trend={campaignSent > 0 ? 'up' : undefined} trendValue={`${campaignTotal} total`} />
              <MetricCard label="Failed" value={campaignFailed} icon={<AlertCircle className="w-5 h-5" />} />
            </div>

            {/* Progress Bar */}
            {campaignTotal > 0 && (
              <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-6">
                <div className="flex items-center gap-4 mb-4">
                  <div className="w-10 h-10 rounded-xl bg-brand-purple-bg border-2 border-ink flex items-center justify-center text-primary"><BarChart3 className="w-5 h-5" /></div>
                  <h3 className="font-clash text-lg font-bold">Campaign Progress</h3>
                </div>
                <div className="w-full h-3 bg-surface-muted rounded-full overflow-hidden border-2 border-ink/10">
                  <div className="h-full flex">
                    <div className="bg-secondary transition-all duration-500" style={{ width: `${campaignTotal > 0 ? (campaignSent / campaignTotal) * 100 : 0}%` }} />
                    <div className="bg-error transition-all duration-500" style={{ width: `${campaignTotal > 0 ? (campaignFailed / campaignTotal) * 100 : 0}%` }} />
                  </div>
                </div>
              </div>
            )}

            {/* Leads Table */}
            <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-6">
              <div className="flex items-center gap-4 mb-4">
                <div className="w-10 h-10 rounded-xl bg-studojo-green-bg border-2 border-ink flex items-center justify-center text-secondary"><Users className="w-5 h-5" /></div>
                <h3 className="font-clash text-lg font-bold">Campaign Leads</h3>
                <Badge variant="primary">{emails.length} leads</Badge>
              </div>
              {emails.length === 0 ? (
                <p className="text-sm text-muted font-satoshi text-center py-6">No emails scheduled yet.</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm font-satoshi">
                    <thead>
                      <tr className="border-b-2 border-ink/10">
                        <th className="text-left py-3 px-2 text-xs font-bold text-muted uppercase">Lead Name</th>
                        <th className="text-left py-3 px-2 text-xs font-bold text-muted uppercase">Email</th>
                        <th className="text-left py-3 px-2 text-xs font-bold text-muted uppercase hidden md:table-cell">Company</th>
                        <th className="text-left py-3 px-2 text-xs font-bold text-muted uppercase">Status</th>
                        <th className="text-left py-3 px-2 text-xs font-bold text-muted uppercase">Schedule</th>
                      </tr>
                    </thead>
                    <tbody>
                      {emails.map((email) => (
                        <tr key={email.id} className="border-b border-ink/5 hover:bg-surface-muted transition-colors">
                          <td className="py-3 px-2 font-bold">{email.lead_name}</td>
                          <td className="py-3 px-2 text-muted truncate max-w-[200px]">{email.to_email}</td>
                          <td className="py-3 px-2 text-muted hidden md:table-cell">{email.lead_company}</td>
                          <td className="py-3 px-2"><StatusBadge status={email.status === 'queued' ? 'queued' : email.status} /></td>
                          <td className="py-3 px-2 text-sm">
                            {email.status === 'sent' && email.sent_at
                              ? <span className="text-secondary text-xs">Sent {formatTimestamp(email.sent_at, tz)}</span>
                              : email.status === 'failed'
                                ? <span className="text-error text-xs">Failed</span>
                                : email.scheduled_at
                                  ? <span className="text-primary text-xs font-medium">{formatTimestamp(email.scheduled_at, tz)}</span>
                                  : <span className="text-muted text-xs">Queued</span>
                            }
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>
        ) : (
          <div className="flex justify-center py-12"><Spinner /></div>
        )}
        </>
        )}
      </Container>
    </div>
  );
}

// ── LinkedIn Dashboard sub-component ──────────────────────────────────────────

function LinkedInDashboard({
  campaign, stats, requests, toggling, onToggle,
}: {
  campaign: any; stats: any; requests: any[]; toggling: boolean; onToggle: () => void;
}) {
  const [filterTab, setFilterTab] = useState<'all' | 'sent' | 'accepted' | 'replied'>('all');

  if (!campaign) {
    return (
      <div className="flex justify-center py-12 animate-fade-in"><Spinner /></div>
    );
  }

  const sent = stats?.total_sent ?? campaign.total_sent ?? 0;
  const accepted = stats?.total_accepted ?? campaign.total_accepted ?? 0;
  const replied = stats?.total_replied ?? campaign.total_replied ?? 0;
  const total = campaign.total_leads ?? requests.length;
  const acceptRate = sent > 0 ? Math.round((accepted / sent) * 100) : 0;

  const filtered = filterTab === 'all' ? requests
    : requests.filter((r) => r.status === filterTab || (filterTab === 'sent' && ['sent', 'accepted', 'followup_sent', 'replied'].includes(r.status)));

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="font-clash text-2xl font-bold">{campaign.name}</h1>
          <span className={`inline-block mt-2 text-xs font-bold px-2 py-0.5 rounded-full ${
            campaign.status === 'running' ? 'bg-green-100 text-green-700'
            : campaign.status === 'paused' ? 'bg-amber-100 text-amber-700'
            : 'bg-gray-100 text-gray-500'
          }`}>
            {campaign.status.charAt(0).toUpperCase() + campaign.status.slice(1)}
          </span>
        </div>
        <Button
          variant={campaign.status === 'running' ? 'outline' : 'primary'}
          onClick={onToggle}
          loading={toggling}
        >
          {campaign.status === 'running'
            ? <><Pause className="w-4 h-4 mr-2 inline" />Pause</>
            : <><Play className="w-4 h-4 mr-2 inline" />Resume</>}
        </Button>
      </div>

      {/* Metrics row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {[
          { label: 'Requests Sent', value: sent },
          { label: 'Accepted', value: accepted },
          { label: 'Acceptance Rate', value: `${acceptRate}%` },
          { label: 'Replies', value: replied },
        ].map(({ label, value }) => (
          <div key={label} className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-4 text-center">
            <p className="text-2xl font-bold font-clash">{value}</p>
            <p className="text-xs text-muted font-satoshi mt-0.5">{label}</p>
          </div>
        ))}
      </div>

      {/* Filter tabs */}
      <div className="flex gap-2">
        {(['all', 'sent', 'accepted', 'replied'] as const).map((t) => (
          <button
            key={t}
            onClick={() => setFilterTab(t)}
            className={`px-3 py-1.5 rounded-lg text-xs font-bold font-satoshi border transition-all ${
              filterTab === t ? 'border-ink bg-white shadow-sm text-primary' : 'border-ink/20 text-muted hover:text-ink'
            }`}
          >
            {t.charAt(0).toUpperCase() + t.slice(1)}
          </button>
        ))}
        <span className="ml-auto text-xs text-muted font-satoshi self-center">{total} total leads</span>
      </div>

      {/* Requests table */}
      <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal overflow-hidden">
        {filtered.length === 0 ? (
          <p className="text-sm text-muted font-satoshi text-center py-8">No requests found.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm font-satoshi">
              <thead>
                <tr className="border-b-2 border-ink/10 bg-surface-muted">
                  <th className="text-left py-3 px-4 text-xs font-bold text-muted uppercase">Name</th>
                  <th className="text-left py-3 px-4 text-xs font-bold text-muted uppercase hidden md:table-cell">Company</th>
                  <th className="text-left py-3 px-4 text-xs font-bold text-muted uppercase">Status</th>
                  <th className="text-left py-3 px-4 text-xs font-bold text-muted uppercase hidden lg:table-cell">Match reason</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((r) => {
                  const s = LI_STATUS[r.status] || { label: r.status, className: 'bg-gray-100 text-gray-500' };
                  return (
                    <tr key={r.id} className="border-b border-ink/5 hover:bg-surface-muted transition-colors">
                      <td className="py-3 px-4">
                        <p className="font-bold">{r.name}</p>
                        {r.headline && <p className="text-xs text-muted truncate max-w-[160px]">{r.headline}</p>}
                      </td>
                      <td className="py-3 px-4 text-muted hidden md:table-cell">{r.company || '—'}</td>
                      <td className="py-3 px-4">
                        <span className={`inline-block text-xs font-bold px-2 py-0.5 rounded-full ${s.className}`}>{s.label}</span>
                      </td>
                      <td className="py-3 px-4 text-xs text-muted hidden lg:table-cell">{r.match_reason || '—'}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
