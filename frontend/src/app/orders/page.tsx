'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { useAppStore } from '@/store/useAppStore';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Spinner } from '@/components/ui/Spinner';
import { ClipboardList, ArrowRight, Clock, CheckCircle, XCircle, Zap } from 'lucide-react';
import api from '@/lib/api';

interface Order {
  id: number;
  status: string;
  candidate_id: number | null;
  campaign_id: number | null;
  email_account_id: number | null;
  leads_collected: number | null;
  leads_target: number | null;
  action_log: Array<{ ts: string; msg: string }>;
  created_at: string | null;
  updated_at: string | null;
}

const STATUS_CONFIG: Record<string, { label: string; variant: 'default' | 'primary' | 'success' | 'warning' }> = {
  created: { label: 'Created', variant: 'default' },
  leads_generating: { label: 'Discovering Leads', variant: 'warning' },
  leads_ready: { label: 'Leads Ready', variant: 'primary' },
  enriching: { label: 'Enriching', variant: 'warning' },
  enrichment_complete: { label: 'Enrichment Done', variant: 'primary' },
  campaign_setup: { label: 'Campaign Setup', variant: 'primary' },
  email_connected: { label: 'Email Connected', variant: 'primary' },
  campaign_running: { label: 'Campaign Running', variant: 'success' },
  completed: { label: 'Completed', variant: 'success' },
};

function StatusIcon({ status }: { status: string }) {
  if (status === 'completed') return <CheckCircle className="w-5 h-5 text-secondary" />;
  if (status === 'campaign_running') return <Zap className="w-5 h-5 text-secondary" />;
  if (['leads_generating', 'enriching'].includes(status)) return <Clock className="w-5 h-5 text-amber-500" />;
  return <ClipboardList className="w-5 h-5 text-muted" />;
}

export default function OrdersPage() {
  const router = useRouter();
  const { loading: authLoading } = useAuth();
  const { setCampaignId, setCandidateId, setEmailAccountId } = useAppStore();
  const [orders, setOrders] = useState<Order[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    api.get('/orders/list')
      .then((res) => {
        setOrders(res.data.orders?.map((o: any) => o.order || o) || []);
      })
      .catch((err) => {
        setError(err.response?.data?.detail || 'Failed to load orders');
      })
      .finally(() => setLoading(false));
  }, []);

  const handleResume = async (orderId: number) => {
    try {
      const res = await api.get(`/orders/${orderId}/resume`);
      const data = res.data;
      if (data.candidate_id) setCandidateId(data.candidate_id);
      if (data.campaign_id) setCampaignId(data.campaign_id);
      if (data.email_account_id) setEmailAccountId(data.email_account_id);
      router.push(data.redirect);
    } catch {
      setError('Failed to resume order');
    }
  };

  if (authLoading || loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-white">
        <Spinner />
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-white">
      <Navbar />
      <Container className="py-8">
        <div className="space-y-6 animate-fade-in">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="font-clash text-2xl font-bold">My Orders</h1>
              <p className="text-sm text-muted font-satoshi mt-1">Track your outreach campaigns</p>
            </div>
            <Button onClick={() => router.push('/onboarding/upload')}>
              New Campaign
            </Button>
          </div>

          {error && (
            <div className="rounded-2xl border-2 border-red-300 bg-red-50 p-4">
              <p className="text-error font-satoshi">{error}</p>
            </div>
          )}

          {orders.length === 0 && !error ? (
            <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-12 text-center">
              <ClipboardList className="w-12 h-12 text-muted mx-auto mb-4" />
              <h3 className="font-clash text-lg font-bold mb-2">No orders yet</h3>
              <p className="text-sm text-muted font-satoshi mb-6">Start your first outreach campaign to see orders here.</p>
              <Button onClick={() => router.push('/onboarding/upload')}>Get Started</Button>
            </div>
          ) : (
            <div className="space-y-4">
              {orders.map((order) => {
                const cfg = STATUS_CONFIG[order.status] || { label: order.status, variant: 'default' as const };
                const isActive = order.status !== 'completed';
                return (
                  <div
                    key={order.id}
                    className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-6 hover:shadow-brutal-lg transition-shadow"
                  >
                    <div className="flex items-start justify-between gap-4">
                      <div className="flex items-start gap-4 flex-1 min-w-0">
                        <div className="w-10 h-10 rounded-xl bg-surface-muted border-2 border-ink flex items-center justify-center flex-shrink-0">
                          <StatusIcon status={order.status} />
                        </div>
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-3 flex-wrap">
                            <h3 className="font-clash text-base font-bold">Order #{order.id}</h3>
                            <Badge variant={cfg.variant}>{cfg.label}</Badge>
                          </div>
                          <div className="flex items-center gap-4 mt-1 text-xs text-muted font-satoshi">
                            {order.created_at && (
                              <span>Created {new Date(order.created_at).toLocaleDateString()}</span>
                            )}
                            {order.leads_collected != null && (
                              <span>{order.leads_collected} leads</span>
                            )}
                            {order.campaign_id && (
                              <span>Campaign #{order.campaign_id}</span>
                            )}
                          </div>
                          {/* Latest log entry */}
                          {order.action_log && order.action_log.length > 0 && (
                            <p className="text-xs text-muted font-satoshi mt-2 truncate">
                              Latest: {order.action_log[order.action_log.length - 1].msg}
                            </p>
                          )}
                        </div>
                      </div>
                      {isActive && (
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => handleResume(order.id)}
                          className="flex-shrink-0"
                        >
                          Resume <ArrowRight className="w-4 h-4 ml-1 inline" />
                        </Button>
                      )}
                      {!isActive && order.campaign_id && (
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => {
                            setCampaignId(order.campaign_id!);
                            router.push('/campaign/dashboard');
                          }}
                          className="flex-shrink-0"
                        >
                          View <ArrowRight className="w-4 h-4 ml-1 inline" />
                        </Button>
                      )}
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
