'use client';

import { useEffect, useState, useCallback } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import {
  Users, CheckCircle, MessageSquare, Send, Pause, Play,
  ExternalLink, Clock, ThumbsUp, Minus, ThumbsDown
} from 'lucide-react';
import api from '@/lib/api';
import type { CampaignStats, ConnectionRequest, LinkedInCampaign } from '@/lib/types/linkedin';

const STATUS_LABELS: Record<string, { label: string; color: string }> = {
  pending:       { label: 'Queued',         color: 'bg-gray-100 text-gray-600' },
  sent:          { label: 'Sent',            color: 'bg-blue-100 text-blue-700' },
  accepted:      { label: 'Accepted',        color: 'bg-green-100 text-green-700' },
  followup_sent: { label: 'Follow-up sent',  color: 'bg-violet-100 text-violet-700' },
  replied:       { label: 'Replied',         color: 'bg-emerald-100 text-emerald-700' },
  error:         { label: 'Error',           color: 'bg-red-100 text-red-700' },
  declined:      { label: 'Declined',        color: 'bg-orange-100 text-orange-700' },
};

const SENTIMENT_ICONS = {
  positive: <ThumbsUp className="w-3.5 h-3.5 text-green-600" />,
  negative: <ThumbsDown className="w-3.5 h-3.5 text-red-500" />,
  neutral:  <Minus className="w-3.5 h-3.5 text-gray-400" />,
};

function MetricCard({ label, value, sub }: { label: string; value: number | string; sub?: string }) {
  return (
    <div className="bg-white border border-border rounded-xl p-4">
      <p className="text-xs text-muted-foreground mb-1">{label}</p>
      <p className="text-2xl font-bold text-foreground">{value}</p>
      {sub && <p className="text-xs text-muted-foreground mt-0.5">{sub}</p>}
    </div>
  );
}

export default function LinkedInDashboardPage() {
  const router = useRouter();
  const params = useSearchParams();
  const campaignId = params.get('id');
  useAuth();

  const [campaign, setCampaign] = useState<LinkedInCampaign | null>(null);
  const [stats, setStats] = useState<CampaignStats | null>(null);
  const [requests, setRequests] = useState<ConnectionRequest[]>([]);
  const [activeTab, setActiveTab] = useState<'all' | 'sent' | 'accepted' | 'replied'>('all');
  const [toggling, setToggling] = useState(false);

  const fetchAll = useCallback(async () => {
    if (!campaignId) return;
    const [cRes, sRes, rRes] = await Promise.all([
      api.get(`/linkedin/automation/campaigns/${campaignId}`),
      api.get(`/linkedin/automation/campaigns/${campaignId}/stats`),
      api.get(`/linkedin/automation/campaigns/${campaignId}/requests?limit=200`),
    ]);
    setCampaign(cRes.data);
    setStats(sRes.data);
    setRequests(rRes.data);
  }, [campaignId]);

  useEffect(() => {
    if (!campaignId) { router.push('/'); return; }
    fetchAll();
    const interval = setInterval(fetchAll, 30000);
    return () => clearInterval(interval);
  }, [campaignId]);

  const toggleCampaign = async () => {
    if (!campaign) return;
    setToggling(true);
    try {
      const action = campaign.status === 'running' ? 'pause' : 'resume';
      await api.post(`/linkedin/automation/campaigns/${campaign.id}/${action}`);
      await fetchAll();
    } finally {
      setToggling(false);
    }
  };

  const filteredRequests = requests.filter(r => {
    if (activeTab === 'all') return true;
    if (activeTab === 'sent') return ['sent', 'accepted', 'followup_sent', 'replied'].includes(r.status);
    if (activeTab === 'accepted') return ['accepted', 'followup_sent', 'replied'].includes(r.status);
    if (activeTab === 'replied') return r.status === 'replied';
    return true;
  });

  if (!campaign || !stats) {
    return (
      <div className="min-h-screen bg-background">
        <Navbar />
        <div className="flex items-center justify-center h-64 text-muted-foreground text-sm">Loading...</div>
      </div>
    );
  }

  const isRunning = campaign.status === 'running';

  return (
    <div className="min-h-screen bg-background">
      <Navbar />
      <Container className="py-8 max-w-3xl">
        {/* Header */}
        <div className="flex items-start justify-between mb-8">
          <div>
            <div className="flex items-center gap-2 mb-1">
              <button onClick={() => router.push('/')} className="text-sm text-muted-foreground hover:text-foreground">
                ← Campaigns
              </button>
            </div>
            <h1 className="text-xl font-bold">{campaign.name}</h1>
            <p className="text-sm text-muted-foreground mt-0.5">{campaign.target_role}</p>
          </div>
          <div className="flex items-center gap-3">
            <span className={`text-xs font-medium px-2.5 py-1 rounded-full ${
              isRunning ? 'bg-green-100 text-green-700' : 'bg-amber-100 text-amber-700'
            }`}>
              {campaign.status}
            </span>
            <Button
              onClick={toggleCampaign}
              disabled={toggling}
              variant={isRunning ? 'outline' : 'primary'}
              className="flex items-center gap-1.5 text-sm"
            >
              {isRunning ? <><Pause className="w-3.5 h-3.5" /> Pause</> : <><Play className="w-3.5 h-3.5" /> Resume</>}
            </Button>
          </div>
        </div>

        {/* Metrics */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-8">
          <MetricCard label="Requests sent" value={stats.total_sent} />
          <MetricCard label="Accepted" value={stats.total_accepted} sub={`${stats.acceptance_rate}% rate`} />
          <MetricCard label="Follow-ups sent" value={stats.total_followups_sent} />
          <MetricCard label="Replies" value={stats.total_replied} sub={`${stats.reply_rate}% of accepted`} />
        </div>

        {/* Requests table */}
        <div className="bg-white border border-border rounded-2xl overflow-hidden">
          <div className="flex border-b border-border">
            {(['all', 'sent', 'accepted', 'replied'] as const).map(tab => (
              <button
                key={tab}
                onClick={() => setActiveTab(tab)}
                className={`px-4 py-3 text-sm font-medium transition-colors ${
                  activeTab === tab ? 'text-primary border-b-2 border-primary' : 'text-muted-foreground hover:text-foreground'
                }`}
              >
                {tab.charAt(0).toUpperCase() + tab.slice(1)}
              </button>
            ))}
          </div>

          {filteredRequests.length === 0 ? (
            <div className="text-center py-12 text-sm text-muted-foreground">
              {activeTab === 'all' ? 'No requests yet. Campaign is warming up.' : `No ${activeTab} requests yet.`}
            </div>
          ) : (
            <div className="divide-y divide-border">
              {filteredRequests.map(req => {
                const s = STATUS_LABELS[req.status] || { label: req.status, color: 'bg-gray-100 text-gray-600' };
                return (
                  <div key={req.id} className="px-4 py-3 flex items-center gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <p className="text-sm font-medium truncate">{req.name}</p>
                        <a href={req.profile_url} target="_blank" rel="noopener noreferrer" className="text-muted-foreground hover:text-foreground flex-shrink-0">
                          <ExternalLink className="w-3 h-3" />
                        </a>
                      </div>
                      {req.headline && <p className="text-xs text-muted-foreground truncate mt-0.5">{req.headline}</p>}
                      {req.reply_text && (
                        <p className="text-xs text-foreground/70 mt-1 line-clamp-1 italic">"{req.reply_text}"</p>
                      )}
                    </div>
                    <div className="flex items-center gap-2 flex-shrink-0">
                      {req.reply_sentiment && SENTIMENT_ICONS[req.reply_sentiment as keyof typeof SENTIMENT_ICONS]}
                      <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${s.color}`}>
                        {s.label}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </Container>
    </div>
  );
}
