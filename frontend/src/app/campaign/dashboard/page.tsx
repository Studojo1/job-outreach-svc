'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { useAppStore } from '@/store/useAppStore';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { MetricCard } from '@/components/features/MetricCard';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Spinner } from '@/components/ui/Spinner';
import { Mail, Send, Reply, AlertCircle, BarChart3, Pause, Play } from 'lucide-react';
import api from '@/lib/api';
import type { CampaignMetrics } from '@/lib/types/campaign';

export default function DashboardPage() {
  const router = useRouter();
  useAuth();
  const { campaignId } = useAppStore();
  const [metrics, setMetrics] = useState<CampaignMetrics | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const fetchMetrics = async () => {
    if (!campaignId) return;
    try {
      const res = await api.get(`/campaign/${campaignId}/metrics`);
      setMetrics(res.data);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to load metrics');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchMetrics();
    const interval = setInterval(fetchMetrics, 30000); // Poll every 30 seconds
    return () => clearInterval(interval);
  }, [campaignId]);

  const handleTransition = async (status: string) => {
    if (!campaignId) return;
    try {
      await api.post(`/campaign/${campaignId}/transition`, { target_status: status });
      fetchMetrics();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to update campaign');
    }
  };


  if (!campaignId) {
    return (
      <div className="min-h-screen bg-page">
        <Navbar />
        <Container className="max-w-onboarding py-xl text-center">
          <p className="text-body-lg text-text-secondary mt-xl">No active campaign.</p>
          <Button onClick={() => router.push('/campaign/setup')} className="mt-l">
            Create Campaign
          </Button>
        </Container>
      </div>
    );
  }

  const statusColor: Record<string, 'primary' | 'success' | 'warning' | 'default'> = {
    draft: 'default',
    running: 'success',
    paused: 'warning',
    completed: 'primary',
  };

  return (
    <div className="min-h-screen bg-page">
      <Navbar />
      <Container className="py-xl">
        {loading ? (
          <div className="flex justify-center py-xxl"><Spinner /></div>
        ) : error ? (
          <div className="card p-xl text-center">
            <p className="text-error">{error}</p>
          </div>
        ) : metrics ? (
          <div className="space-y-xl animate-fade-in">
            {/* Header */}
            <div className="flex items-center justify-between">
              <div>
                <h1 className="text-h2">{metrics.campaign_name}</h1>
                <Badge variant={statusColor[metrics.status] || 'default'} className="mt-s">
                  {metrics.status.charAt(0).toUpperCase() + metrics.status.slice(1)}
                </Badge>
              </div>
              <div className="flex gap-m">
                {metrics.status === 'running' && (
                  <Button variant="outline" onClick={() => handleTransition('paused')}>
                    <Pause className="w-4 h-4 mr-s inline" /> Pause
                  </Button>
                )}
                {metrics.status === 'paused' && (
                  <Button onClick={() => handleTransition('running')}>
                    <Play className="w-4 h-4 mr-s inline" /> Resume
                  </Button>
                )}
              </div>
            </div>

            {/* Metric Cards */}
            <div className="grid grid-cols-1 md:grid-cols-4 gap-l">
              <MetricCard
                label="Emails Sent"
                value={metrics.emails_sent}
                icon={<Send className="w-5 h-5" />}
                trend="up"
                trendValue={`${metrics.emails_total} total`}
              />
              <MetricCard
                label="Open Rate"
                value="--"
                icon={<Mail className="w-5 h-5" />}
              />
              <MetricCard
                label="Reply Rate"
                value={`${metrics.reply_rate}%`}
                icon={<Reply className="w-5 h-5" />}
                trend={metrics.reply_rate > 0 ? 'up' : undefined}
                trendValue={`${metrics.emails_replied} replies`}
              />
              <MetricCard
                label="Bounces"
                value={metrics.emails_failed}
                icon={<AlertCircle className="w-5 h-5" />}
              />
            </div>

            {/* Activity Summary */}
            {metrics.emails_sent === 0 && metrics.emails_replied === 0 ? (
              <div className="card p-xl text-center">
                <Mail className="w-10 h-10 text-text-secondary mx-auto mb-m" />
                <p className="text-body-lg text-text-secondary">No campaign activity yet.</p>
                <p className="text-body text-text-secondary mt-xs">
                  Emails will appear here once sending begins.
                </p>
              </div>
            ) : null}

            {/* Queue Status */}
            <div className="card p-l">
              <div className="flex items-center gap-m mb-m">
                <BarChart3 className="w-5 h-5 text-primary" />
                <h3 className="text-h3">Queue Status</h3>
              </div>
              <div className="grid grid-cols-3 gap-l text-center">
                <div>
                  <p className="text-h2 text-primary">{metrics.emails_queued}</p>
                  <p className="text-label text-text-secondary mt-xs">Queued</p>
                </div>
                <div>
                  <p className="text-h2 text-secondary">{metrics.emails_sent}</p>
                  <p className="text-label text-text-secondary mt-xs">Sent</p>
                </div>
                <div>
                  <p className="text-h2 text-error">{metrics.emails_failed}</p>
                  <p className="text-label text-text-secondary mt-xs">Failed</p>
                </div>
              </div>
              {/* Progress bar */}
              <div className="w-full h-3 bg-gray-100 rounded-full mt-l overflow-hidden">
                <div className="h-full flex">
                  <div
                    className="bg-secondary transition-all duration-500"
                    style={{ width: `${metrics.emails_total > 0 ? (metrics.emails_sent / metrics.emails_total) * 100 : 0}%` }}
                  />
                  <div
                    className="bg-error transition-all duration-500"
                    style={{ width: `${metrics.emails_total > 0 ? (metrics.emails_failed / metrics.emails_total) * 100 : 0}%` }}
                  />
                </div>
              </div>
            </div>
          </div>
        ) : null}
      </Container>
    </div>
  );
}