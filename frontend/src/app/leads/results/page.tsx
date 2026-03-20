'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { useAppStore } from '@/store/useAppStore';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { FlashCard } from '@/components/features/FlashCard';
import { Button } from '@/components/ui/Button';
import { Spinner } from '@/components/ui/Spinner';
import { ArrowRight, ArrowLeft, Filter, Mail } from 'lucide-react';
import api from '@/lib/api';
import type { Lead } from '@/lib/types/lead';

const PAGE_SIZE = 20;

export default function ResultsPage() {
  const router = useRouter();
  useAuth();
  const { candidateId } = useAppStore();
  const [leads, setLeads] = useState<Lead[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [sortBy, setSortBy] = useState<'score' | 'name'>('score');
  const [page, setPage] = useState(1);

  useEffect(() => {
    if (!candidateId) return;
    api.get(`/candidate/${candidateId}/leads`)
      .then((res) => setLeads(res.data.leads || res.data))
      .catch((err) => setError(err.response?.data?.detail || 'Failed to load leads'))
      .finally(() => setLoading(false));
  }, [candidateId]);

  const sorted = [...leads].sort((a, b) => {
    if (sortBy === 'score') return (b.score?.overall || 0) - (a.score?.overall || 0);
    return (a.name || '').localeCompare(b.name || '');
  });

  const totalPages = Math.ceil(sorted.length / PAGE_SIZE);
  const paginated = sorted.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  if (!candidateId) {
    router.push('/onboarding/upload');
    return null;
  }

  return (
    <div className="min-h-screen bg-white">
      <Navbar />
      <Container className="py-8">
        {/* Header */}
        <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between mb-6 gap-4">
          <div>
            <h1 className="font-clash text-2xl font-bold">Lead Results</h1>
            <p className="text-sm text-muted font-satoshi mt-1">
              {leads.length} decision makers found. Click cards to see scoring details.
            </p>
          </div>
          <div className="flex items-center gap-3 flex-wrap">
            <Button size="sm" onClick={() => router.push('/enrichment')}>
              <Mail className="w-4 h-4 mr-1.5" /> Enrich Leads
            </Button>
            <div className="flex items-center gap-2">
              <Filter className="w-4 h-4 text-muted" />
              <select
                value={sortBy}
                onChange={(e) => { setSortBy(e.target.value as 'score' | 'name'); setPage(1); }}
                className="text-sm border-2 border-ink/20 rounded-xl px-3 py-1.5 bg-white focus:outline-none focus:ring-2 focus:ring-primary font-satoshi"
              >
                <option value="score">Highest Score</option>
                <option value="name">Name (A-Z)</option>
              </select>
            </div>
          </div>
        </div>

        {loading ? (
          <div className="flex justify-center py-20"><Spinner /></div>
        ) : error ? (
          <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-8 text-center">
            <p className="text-error font-satoshi">{error}</p>
          </div>
        ) : (
          <>
            {/* Lead grid — 2/3/4 columns responsive */}
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
              {paginated.map((lead) => (
                <FlashCard key={lead.id} lead={lead} />
              ))}
            </div>

            {/* Pagination */}
            {totalPages > 1 && (
              <div className="flex items-center justify-center gap-2 mt-8">
                <button
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={page === 1}
                  className="p-2 rounded-xl border-2 border-ink/20 hover:bg-surface-muted disabled:opacity-30 disabled:cursor-not-allowed"
                >
                  <ArrowLeft className="w-4 h-4" />
                </button>
                {Array.from({ length: Math.min(totalPages, 7) }, (_, i) => {
                  let pageNum: number;
                  if (totalPages <= 7) {
                    pageNum = i + 1;
                  } else if (page <= 4) {
                    pageNum = i + 1;
                  } else if (page >= totalPages - 3) {
                    pageNum = totalPages - 6 + i;
                  } else {
                    pageNum = page - 3 + i;
                  }
                  return (
                    <button
                      key={pageNum}
                      onClick={() => setPage(pageNum)}
                      className={`w-9 h-9 rounded-xl text-sm font-bold font-satoshi transition-colors ${
                        page === pageNum
                          ? 'bg-primary text-white border-2 border-ink'
                          : 'border-2 border-ink/20 hover:bg-surface-muted text-muted'
                      }`}
                    >
                      {pageNum}
                    </button>
                  );
                })}
                <button
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  disabled={page === totalPages}
                  className="p-2 rounded-xl border-2 border-ink/20 hover:bg-surface-muted disabled:opacity-30 disabled:cursor-not-allowed"
                >
                  <ArrowRight className="w-4 h-4" />
                </button>
              </div>
            )}

            {/* Bottom enrichment CTA */}
            {leads.length > 0 && (
              <div className="text-center mt-8">
                <Button size="lg" onClick={() => router.push('/enrichment')}>
                  Enrich Emails <ArrowRight className="w-4 h-4 ml-2 inline" />
                </Button>
              </div>
            )}
          </>
        )}
      </Container>
    </div>
  );
}
