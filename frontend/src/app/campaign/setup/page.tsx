'use client';

import { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { useAppStore } from '@/store/useAppStore';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Input } from '@/components/ui/Input';
import { Spinner } from '@/components/ui/Spinner';
import { Shield, Clock, Mail, Zap, CheckCircle, Eye } from 'lucide-react';
import api from '@/lib/api';

export default function CampaignSetupPage() {
  const router = useRouter();
  useAuth();
  const { candidateId, emailAccountId, selectedTemplate, selectedStyles } = useAppStore();
  const [campaignName, setCampaignName] = useState('My Outreach Campaign');
  const [launching, setLaunching] = useState(false);
  const [error, setError] = useState('');
  const [previewEmail, setPreviewEmail] = useState<{ subject: string; body: string; lead_name: string; company: string } | null>(null);
  const [previewLoading, setPreviewLoading] = useState(true);

  const safeSettings = [
    { icon: <Mail className="w-4 h-4" />, label: 'Daily limit', value: '5-7 emails/day' },
    { icon: <Clock className="w-4 h-4" />, label: 'Sending hours', value: '9 AM - 6 PM' },
    { icon: <Zap className="w-4 h-4" />, label: 'Gap between emails', value: '40-90 minutes (randomized)' },
    { icon: <Shield className="w-4 h-4" />, label: 'First email', value: 'Within 3 minutes of launch' },
  ];

  // Fetch a sample email preview for the user to see quality before launching
  useEffect(() => {
    if (!candidateId) return;

    const fetchPreview = async () => {
      setPreviewLoading(true);
      try {
        const res = await api.post('/campaign/preview-email', {
          candidate_id: candidateId,
          selected_styles: selectedStyles.length > 0 ? selectedStyles : ['value_prop'],
        });
        setPreviewEmail(res.data);
      } catch {
        // If preview endpoint doesn't exist yet, show the template as fallback
        if (selectedTemplate) {
          setPreviewEmail({
            subject: selectedTemplate.subject,
            body: selectedTemplate.body,
            lead_name: 'Sample Lead',
            company: 'Example Company',
          });
        }
      } finally {
        setPreviewLoading(false);
      }
    };

    fetchPreview();
  }, [candidateId]);

  const handleLaunch = async () => {
    if (!candidateId || !emailAccountId) return;
    setLaunching(true);
    setError('');

    try {
      // Pre-launch validation
      const validationRes = await api.get(`/campaign/validate?candidate_id=${candidateId}&email_account_id=${emailAccountId}`);
      if (!validationRes.data.valid) {
        setError(validationRes.data.reason || 'Validation failed. Please check your setup.');
        setLaunching(false);
        return;
      }
    } catch (err: any) {
      // If validation endpoint returns 404, skip validation (backward compat)
      if (err.response?.status !== 404) {
        setError(err.response?.data?.detail || 'Validation failed');
        setLaunching(false);
        return;
      }
    }

    // Store launch params and redirect to progress screen
    sessionStorage.setItem('campaign_launch', JSON.stringify({
      campaignName,
      selectedStyles: selectedStyles.length > 0 ? selectedStyles : [],
      selectedTemplate: selectedStyles.length === 0 ? selectedTemplate : null,
    }));
    router.push('/campaign/launching');
  };

  return (
    <div className="min-h-screen bg-page">
      <Navbar />
      <Container className="max-w-onboarding py-xl">
        <h1 className="text-h2 mb-s">Campaign Setup</h1>
        <p className="text-body-sm text-text-secondary mb-xl">
          Review your email preview and settings before launching.
        </p>

        <div className="space-y-l">
          {/* Campaign Name */}
          <div className="card p-l">
            <label className="text-label text-text-secondary block mb-s">Campaign Name</label>
            <Input
              value={campaignName}
              onChange={(e) => setCampaignName(e.target.value)}
              placeholder="My Outreach Campaign"
            />
          </div>

          {/* Email Preview */}
          <div className="card p-l">
            <div className="flex items-center gap-m mb-m">
              <Eye className="w-5 h-5 text-primary" />
              <h3 className="text-h3">Email Preview</h3>
              <Badge variant="primary">Sample</Badge>
            </div>
            <p className="text-body-sm text-text-secondary mb-m">
              Here is a sample of what your outreach emails will look like. Each lead receives a unique, personalized email.
            </p>

            {previewLoading ? (
              <div className="flex justify-center py-l"><Spinner /></div>
            ) : previewEmail ? (
              <div className="bg-gray-50 rounded-lg p-l border border-gray-200">
                <div className="mb-m pb-m border-b border-gray-200">
                  <p className="text-label text-text-secondary">To: {previewEmail.lead_name} at {previewEmail.company}</p>
                </div>
                <p className="text-body-sm font-semibold mb-m">
                  Subject: {previewEmail.subject}
                </p>
                <p className="text-body-sm text-text-secondary whitespace-pre-line leading-relaxed">
                  {previewEmail.body}
                </p>
              </div>
            ) : (
              <div className="bg-gray-50 rounded-lg p-l text-center">
                <p className="text-body-sm text-text-secondary">
                  Email preview will be generated when the campaign starts.
                </p>
              </div>
            )}
          </div>

          {/* Safe Sending Settings */}
          <div className="card p-l">
            <div className="flex items-center gap-m mb-m">
              <Shield className="w-5 h-5 text-secondary" />
              <h3 className="text-h3">Safe Sending Settings</h3>
            </div>
            <p className="text-body-sm text-text-secondary mb-m">
              These settings protect your email reputation. They cannot be changed.
            </p>
            <div className="space-y-m">
              {safeSettings.map((s, i) => (
                <div key={i} className="flex items-center justify-between p-m bg-gray-50 rounded-lg">
                  <div className="flex items-center gap-m">
                    <span className="text-primary">{s.icon}</span>
                    <span className="text-body-sm">{s.label}</span>
                  </div>
                  <span className="text-body-sm font-semibold">{s.value}</span>
                </div>
              ))}
            </div>
          </div>

          {error && (
            <p className="text-error text-body-sm text-center">{error}</p>
          )}

          {/* Launch Button */}
          <div className="text-center pt-l">
            <Button size="lg" onClick={handleLaunch} loading={launching}>
              <CheckCircle className="w-5 h-5 mr-s inline" />
              Launch Campaign
            </Button>
          </div>
        </div>
      </Container>
    </div>
  );
}