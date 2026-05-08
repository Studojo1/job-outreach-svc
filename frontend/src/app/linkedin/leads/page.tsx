'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { Button } from '@/components/ui/Button';
import { Spinner } from '@/components/ui/Spinner';
import { Users, Search, ArrowRight, ExternalLink } from 'lucide-react';
import api from '@/lib/api';
import type { ConnectionRequest } from '@/lib/types/linkedin';

export default function LinkedInLeadsPage() {
  const router = useRouter();
  useAuth();

  const [campaignId, setCampaignId] = useState<number | null>(null);
  const [leads, setLeads] = useState<ConnectionRequest[]>([]);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState('');
  const [quizData, setQuizData] = useState<any>(null);

  useEffect(() => {
    const raw = sessionStorage.getItem('li_quiz');
    if (!raw) { router.push('/linkedin/quiz'); return; }
    setQuizData(JSON.parse(raw));
  }, []);

  useEffect(() => {
    if (!quizData) return;
    createCampaignAndSearch();
  }, [quizData]);

  const createCampaignAndSearch = async () => {
    setSearching(true);
    setError('');
    try {
      // Create the campaign from quiz data
      const res = await api.post('/linkedin/automation/campaigns', {
        name: quizData.campaign_name,
        target_role: quizData.target_role,
        target_industries: quizData.target_industries,
        target_locations: quizData.target_locations,
        target_company_sizes: quizData.target_company_sizes,
        target_keywords: quizData.target_keywords || null,
        daily_limit: 20,
      });
      const id = res.data.id;
      setCampaignId(id);
      sessionStorage.setItem('li_campaign_id', String(id));

      // Kick off lead search
      await api.post(`/linkedin/automation/campaigns/${id}/search-leads`);

      // Poll for leads
      await pollLeads(id);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Something went wrong. Please try again.');
      setSearching(false);
    }
  };

  const pollLeads = async (id: number) => {
    const maxAttempts = 20;
    let attempts = 0;
    while (attempts < maxAttempts) {
      await new Promise(r => setTimeout(r, 3000));
      try {
        const res = await api.get(`/linkedin/automation/campaigns/${id}/requests`);
        if (res.data.length > 0) {
          setLeads(res.data);
          setSearching(false);
          return;
        }
      } catch {}
      attempts++;
    }
    setSearching(false);
    setError('No leads found. Try adjusting your search criteria.');
  };

  const handleContinue = () => {
    router.push('/linkedin/setup');
  };

  return (
    <div className="min-h-screen bg-background">
      <Navbar />
      <Container className="py-10 max-w-2xl">
        <div className="mb-8">
          <h1 className="text-2xl font-bold mb-1">Found leads</h1>
          <p className="text-muted-foreground text-sm">
            These are the people we found on LinkedIn matching your criteria.
          </p>
        </div>

        {searching && (
          <div className="text-center py-16">
            <div className="w-12 h-12 bg-primary/10 rounded-full flex items-center justify-center mx-auto mb-4">
              <Search className="w-6 h-6 text-primary animate-pulse" />
            </div>
            <h2 className="font-semibold mb-1">Searching LinkedIn...</h2>
            <p className="text-sm text-muted-foreground">Finding {quizData?.target_role}s in your target markets</p>
          </div>
        )}

        {error && !searching && (
          <div className="bg-red-50 border border-red-200 rounded-xl p-4 text-sm text-red-700 mb-6">
            {error}
          </div>
        )}

        {!searching && leads.length > 0 && (
          <>
            <div className="flex items-center gap-2 mb-4">
              <Users className="w-4 h-4 text-primary" />
              <span className="text-sm font-medium">{leads.length} leads found</span>
            </div>

            <div className="space-y-2 mb-8">
              {leads.slice(0, 15).map(lead => (
                <div key={lead.id} className="bg-white border border-border rounded-xl p-4 flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <p className="font-medium text-sm truncate">{lead.name}</p>
                    {lead.headline && (
                      <p className="text-xs text-muted-foreground mt-0.5 line-clamp-1">{lead.headline}</p>
                    )}
                    {lead.company && (
                      <p className="text-xs text-muted-foreground">{lead.company}</p>
                    )}
                  </div>
                  <a
                    href={lead.profile_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="flex-shrink-0 text-muted-foreground hover:text-foreground"
                    onClick={e => e.stopPropagation()}
                  >
                    <ExternalLink className="w-3.5 h-3.5" />
                  </a>
                </div>
              ))}
              {leads.length > 15 && (
                <p className="text-center text-sm text-muted-foreground pt-2">
                  + {leads.length - 15} more leads
                </p>
              )}
            </div>

            <Button onClick={handleContinue} className="w-full">
              Set up messages <ArrowRight className="w-4 h-4 ml-1" />
            </Button>
          </>
        )}
      </Container>
    </div>
  );
}
