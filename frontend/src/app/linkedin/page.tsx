'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Linkedin, Plus, Users, CheckCircle, MessageSquare, ArrowRight } from 'lucide-react';
import api from '@/lib/api';
import type { LinkedInCampaign } from '@/lib/types/linkedin';

const STATUS_COLORS: Record<string, string> = {
  running: 'bg-green-100 text-green-700',
  paused: 'bg-amber-100 text-amber-700',
  draft: 'bg-gray-100 text-gray-600',
  completed: 'bg-blue-100 text-blue-700',
};

export default function LinkedInHomePage() {
  const router = useRouter();
  useAuth();
  const [campaigns, setCampaigns] = useState<LinkedInCampaign[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.get('/linkedin/automation/campaigns')
      .then(r => setCampaigns(r.data))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="min-h-screen bg-background">
      <Navbar />
      <Container className="py-10 max-w-3xl">
        <div className="flex items-center justify-between mb-8">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 bg-[#0077B5] rounded-lg flex items-center justify-center">
              <Linkedin className="w-5 h-5 text-white" />
            </div>
            <div>
              <h1 className="text-2xl font-bold text-foreground">LinkedIn Outreach</h1>
              <p className="text-sm text-muted-foreground">Automated connection requests + follow-ups</p>
            </div>
          </div>
          <Button onClick={() => router.push('/linkedin/quiz')} className="flex items-center gap-2">
            <Plus className="w-4 h-4" /> New Campaign
          </Button>
        </div>

        {loading ? (
          <div className="text-center py-16 text-muted-foreground">Loading...</div>
        ) : campaigns.length === 0 ? (
          <div className="border border-dashed border-border rounded-2xl p-12 text-center">
            <Linkedin className="w-10 h-10 text-muted-foreground mx-auto mb-4" />
            <h2 className="text-lg font-semibold mb-2">No campaigns yet</h2>
            <p className="text-muted-foreground text-sm mb-6 max-w-sm mx-auto">
              Tell us who you want to reach, connect your LinkedIn, and we'll handle the outreach.
            </p>
            <Button onClick={() => router.push('/linkedin/quiz')}>
              Start your first campaign <ArrowRight className="w-4 h-4 ml-2" />
            </Button>
          </div>
        ) : (
          <div className="space-y-3">
            {campaigns.map(c => (
              <button
                key={c.id}
                onClick={() => router.push(`/linkedin/dashboard?id=${c.id}`)}
                className="w-full text-left bg-white border border-border rounded-xl p-5 hover:border-primary/40 hover:shadow-sm transition-all"
              >
                <div className="flex items-start justify-between mb-3">
                  <div>
                    <h3 className="font-semibold text-foreground">{c.name}</h3>
                    <p className="text-sm text-muted-foreground mt-0.5">{c.target_role}</p>
                  </div>
                  <span className={`text-xs font-medium px-2.5 py-1 rounded-full ${STATUS_COLORS[c.status] || STATUS_COLORS.draft}`}>
                    {c.status}
                  </span>
                </div>
                <div className="flex gap-6 text-sm">
                  <div className="flex items-center gap-1.5 text-muted-foreground">
                    <Users className="w-3.5 h-3.5" />
                    <span>{c.total_sent} sent</span>
                  </div>
                  <div className="flex items-center gap-1.5 text-muted-foreground">
                    <CheckCircle className="w-3.5 h-3.5" />
                    <span>{c.total_accepted} accepted</span>
                  </div>
                  <div className="flex items-center gap-1.5 text-muted-foreground">
                    <MessageSquare className="w-3.5 h-3.5" />
                    <span>{c.total_replied} replied</span>
                  </div>
                </div>
              </button>
            ))}
          </div>
        )}
      </Container>
    </div>
  );
}
