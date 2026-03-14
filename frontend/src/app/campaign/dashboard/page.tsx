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
import { Send, AlertCircle, BarChart3, Pause, Play, Users, CheckCircle, XCircle, Clock, FlaskConical } from 'lucide-react';
import api from '@/lib/api';
import { formatTimestamp } from '@/lib/formatTime';
import type { CampaignMetrics } from '@/lib/types/campaign';

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
  const { campaignId, setCampaignId, user } = useAppStore();
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

  const handleTransition = async (status: string) => {
    if (!campaignId) return;
    try {
      await api.post(`/campaign/${campaignId}/transition`, { target_status: status });
      fetchCampaignData();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to update campaign');
    }
  };

  if (authLoading || loading) return <div className="min-h-screen flex items-center justify-center bg-surface-muted"><Spinner /></div>;

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
      <div className="min-h-screen bg-surface-muted">
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
      <div className="min-h-screen bg-surface-muted">
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
    <div className="min-h-screen bg-surface-muted">
      <Navbar />
      <Container className="py-8">
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
      </Container>
    </div>
  );
}
