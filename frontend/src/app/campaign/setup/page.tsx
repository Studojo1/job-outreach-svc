'use client';

import { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { useOrder } from '@/lib/hooks/useOrder';
import { useAppStore } from '@/store/useAppStore';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Input } from '@/components/ui/Input';
import { Spinner } from '@/components/ui/Spinner';
import { Shield, Clock, Mail, Zap, CheckCircle, Eye, FlaskConical, Pencil } from 'lucide-react';
import api from '@/lib/api';

interface TestEmail {
  index: number;
  lead_name: string;
  lead_company: string;
  original_email: string;
  subject: string;
  body: string;
}

export default function CampaignSetupPage() {
  const router = useRouter();
  const { loading } = useAuth();
  const { candidateId, emailAccountId, selectedTemplate, selectedStyles } = useAppStore();
  const { updateOrder } = useOrder();
  const [campaignName, setCampaignName] = useState('My Outreach Campaign');
  const [launching, setLaunching] = useState(false);
  const [error, setError] = useState('');
  const [previewEmail, setPreviewEmail] = useState<{ subject: string; body: string; lead_name: string; company: string } | null>(null);
  const [previewLoading, setPreviewLoading] = useState(true);

  // Test launch state
  const [testEmails, setTestEmails] = useState<TestEmail[]>([]);
  const [testEmailsLoading, setTestEmailsLoading] = useState(false);
  const [overrides, setOverrides] = useState<Record<number, string>>({});
  const [testLaunching, setTestLaunching] = useState(false);

  const safeSettings = [
    { icon: <Mail className="w-4 h-4" />, label: 'Daily limit', value: '5-7 emails/day' },
    { icon: <Clock className="w-4 h-4" />, label: 'Sending hours', value: '9 AM - 6 PM' },
    { icon: <Zap className="w-4 h-4" />, label: 'Gap between emails', value: '40-90 minutes (randomized)' },
    { icon: <Shield className="w-4 h-4" />, label: 'First email', value: 'Within 3 minutes of launch' },
  ];

  useEffect(() => {
    if (!candidateId) return;
    // Update order status to campaign_setup
    updateOrder({ status: 'campaign_setup', log_entry: 'Entered campaign setup' });
    const fetchPreview = async () => {
      setPreviewLoading(true);
      try {
        const res = await api.post('/campaign/preview-email', { candidate_id: candidateId, selected_styles: selectedStyles.length > 0 ? selectedStyles : ['value_prop'] });
        setPreviewEmail(res.data);
      } catch {
        if (selectedTemplate) setPreviewEmail({ subject: selectedTemplate.subject, body: selectedTemplate.body, lead_name: 'Sample Lead', company: 'Example Company' });
      } finally { setPreviewLoading(false); }
    };
    fetchPreview();
  }, [candidateId]);

  const loadTestEmails = async () => {
    if (!candidateId || !emailAccountId) return;
    setTestEmailsLoading(true); setError('');
    try {
      const res = await api.post('/campaign/test-launch/preview', {
        candidate_id: candidateId,
        email_account_id: emailAccountId,
        selected_styles: selectedStyles.length > 0 ? selectedStyles : ['value_prop'],
      });
      setTestEmails(res.data.emails);
      setOverrides({});
      /* reset */;
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to load test emails');
    } finally { setTestEmailsLoading(false); }
  };

  const handleTestLaunch = async () => {
    if (!candidateId || !emailAccountId) return;
    setTestLaunching(true); setError('');

    const overrideList = Object.entries(overrides)
      .filter(([, email]) => email.trim().length > 0)
      .map(([idx, email]) => ({ lead_index: parseInt(idx), override_email: email.trim() }));

    try {
      // Start background job immediately — returns job_id with leads
      const res = await api.post('/campaign/test-launch', {
        candidate_id: candidateId,
        email_account_id: emailAccountId,
        overrides: overrideList,
        selected_styles: selectedStyles.length > 0 ? selectedStyles : ['value_prop'],
      });

      const jobId = res.data.job_id;
      if (!jobId) throw new Error('No job_id returned');

      // Store job_id and redirect straight to dashboard
      sessionStorage.setItem('test_job_id', jobId);
      sessionStorage.setItem('test_started_at', new Date().toISOString());
      router.push('/campaign/dashboard');
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to start test launch');
      setTestLaunching(false);
    }
  };

  const handleLaunch = async () => {
    if (!candidateId || !emailAccountId) return;
    setLaunching(true); setError('');
    try {
      const validationRes = await api.get(`/campaign/validate?candidate_id=${candidateId}&email_account_id=${emailAccountId}`);
      if (!validationRes.data.valid) { setError(validationRes.data.reason || 'Validation failed.'); setLaunching(false); return; }
    } catch (err: any) {
      if (err.response?.status !== 404) { setError(err.response?.data?.detail || 'Validation failed'); setLaunching(false); return; }
    }
    sessionStorage.setItem('campaign_launch', JSON.stringify({
      campaignName, selectedStyles: selectedStyles.length > 0 ? selectedStyles : [],
      selectedTemplate: selectedStyles.length === 0 ? selectedTemplate : null,
    }));
    router.push('/campaign/launching');
  };

  if (loading) return <div className="min-h-screen flex items-center justify-center bg-white"><Spinner /></div>;

  return (
    <div className="min-h-screen bg-white">
      <Navbar />
      <Container className="max-w-onboarding py-8">
        <h1 className="font-clash text-2xl font-bold mb-2">Campaign Setup</h1>
        <p className="text-sm text-muted font-satoshi mb-8">Review your email preview and settings before launching.</p>

        <div className="space-y-6">
          <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-6">
            <label className="text-xs font-bold text-muted block mb-2 font-satoshi uppercase">Campaign Name</label>
            <Input value={campaignName} onChange={(e) => setCampaignName(e.target.value)} placeholder="My Outreach Campaign" />
          </div>

          <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-6">
            <div className="flex items-center gap-4 mb-4">
              <div className="w-10 h-10 rounded-xl bg-brand-purple-bg border-2 border-ink flex items-center justify-center text-primary"><Eye className="w-5 h-5" /></div>
              <h3 className="font-clash text-lg font-bold">Email Preview</h3>
              <Badge variant="primary">Sample</Badge>
            </div>
            <p className="text-sm text-muted font-satoshi mb-4">Here is a sample of what your outreach emails will look like.</p>
            {previewLoading ? (
              <div className="flex justify-center py-6"><Spinner /></div>
            ) : previewEmail ? (
              <div className="bg-surface-muted rounded-xl border-2 border-ink/20 p-6">
                <div className="mb-4 pb-4 border-b-2 border-ink/10">
                  <p className="text-xs text-muted font-satoshi">To: {previewEmail.lead_name} at {previewEmail.company}</p>
                </div>
                <p className="text-sm font-bold mb-4 font-satoshi">Subject: {previewEmail.subject}</p>
                <p className="text-sm text-muted whitespace-pre-line leading-relaxed font-satoshi">{previewEmail.body}</p>
              </div>
            ) : (
              <div className="bg-surface-muted rounded-xl p-6 text-center">
                <p className="text-sm text-muted font-satoshi">Email preview will be generated when the campaign starts.</p>
              </div>
            )}
          </div>

          <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-6">
            <div className="flex items-center gap-4 mb-4">
              <div className="w-10 h-10 rounded-xl bg-studojo-green-bg border-2 border-ink flex items-center justify-center text-secondary"><Shield className="w-5 h-5" /></div>
              <h3 className="font-clash text-lg font-bold">Safe Sending Settings</h3>
            </div>
            <p className="text-sm text-muted font-satoshi mb-4">These settings protect your email reputation. They cannot be changed.</p>
            <div className="space-y-3">
              {safeSettings.map((s, i) => (
                <div key={i} className="flex items-center justify-between p-4 bg-surface-muted rounded-xl border-2 border-ink/20">
                  <div className="flex items-center gap-3">
                    <span className="text-primary">{s.icon}</span>
                    <span className="text-sm font-satoshi">{s.label}</span>
                  </div>
                  <span className="text-sm font-bold font-satoshi">{s.value}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Test Launch Section */}
          <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-6">
            <div className="flex items-center gap-4 mb-4">
              <div className="w-10 h-10 rounded-xl bg-studojo-orange-bg border-2 border-ink flex items-center justify-center text-amber-600"><FlaskConical className="w-5 h-5" /></div>
              <h3 className="font-clash text-lg font-bold">Deliverability Test</h3>
              <Badge variant="warning">5 emails</Badge>
            </div>
            <p className="text-sm text-muted font-satoshi mb-4">
              Send 5 quick test emails to your own inbox to confirm your email connection and deliverability before launching the campaign.
            </p>

            {testEmails.length === 0 ? (
              <Button variant="outline" onClick={loadTestEmails} loading={testEmailsLoading} className="w-full">
                <FlaskConical className="w-4 h-4 mr-2 inline" /> Load Test Emails
              </Button>
            ) : (
              <>
                <div className="space-y-3 mb-4">
                  {testEmails.map((email) => (
                    <div key={email.index} className="bg-surface-muted rounded-xl border-2 border-ink/20 p-4">
                      <div className="flex items-start justify-between mb-2">
                        <div className="min-w-0 flex-1">
                          <p className="text-sm font-bold font-satoshi truncate">{email.lead_name}</p>
                          <p className="text-xs text-muted font-satoshi truncate">{email.lead_company}</p>
                          <p className="text-xs text-muted font-satoshi mt-1 truncate">Subject: {email.subject}</p>
                        </div>
                        <Badge variant={overrides[email.index] ? 'warning' : 'success'}>
                          {overrides[email.index] ? 'Override' : 'Original'}
                        </Badge>
                      </div>
                      <div className="flex items-center gap-2 mt-2">
                        <Pencil className="w-3 h-3 text-muted flex-shrink-0" />
                        <Input
                          value={overrides[email.index] || ''}
                          onChange={(e) => setOverrides(prev => ({ ...prev, [email.index]: e.target.value }))}
                          placeholder={email.original_email}
                          className="text-xs"
                        />
                      </div>
                    </div>
                  ))}
                </div>

                <Button variant="outline" onClick={handleTestLaunch} loading={testLaunching} className="w-full">
                  <FlaskConical className="w-4 h-4 mr-2 inline" /> Send Test Emails
                </Button>
                {testLaunching && <p className="text-xs text-muted text-center mt-2 font-satoshi">Redirecting to launch screen...</p>}
              </>
            )}
          </div>

          {error && <p className="text-error text-sm text-center font-satoshi">{error}</p>}

          <div className="text-center pt-6">
            <Button size="lg" onClick={handleLaunch} loading={launching}>
              <CheckCircle className="w-5 h-5 mr-2 inline" /> Launch Campaign
            </Button>
          </div>
        </div>
      </Container>
    </div>
  );
}
