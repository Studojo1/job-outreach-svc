'use client';

import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { useAppStore } from '@/store/useAppStore';
import { useOrder } from '@/lib/hooks/useOrder';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { Spinner } from '@/components/ui/Spinner';
import { LinkedInConnectPanel } from '@/components/features/LinkedInConnectPanel';
import { Linkedin } from 'lucide-react';

export default function LinkedInConnectPage() {
  const router = useRouter();
  const { loading } = useAuth();
  const { orderId, setLinkedInCampaignId } = useAppStore();
  const { updateOrder } = useOrder();

  const handleSuccess = async (campaignId: number) => {
    setLinkedInCampaignId(campaignId);
    await updateOrder({
      linkedin_campaign_id: campaignId,
      linkedin_connected: true,
      log_entry: 'LinkedIn connected and campaign created',
    });
    router.push('/campaign/dashboard');
  };

  if (loading) {
    return <div className="min-h-screen flex items-center justify-center bg-white"><Spinner /></div>;
  }

  if (!orderId) {
    router.push('/onboarding/upload');
    return null;
  }

  return (
    <div className="min-h-screen bg-white">
      <Navbar />
      <Container className="max-w-onboarding py-8">
        <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-8">
          {/* Header */}
          <div className="text-center mb-8">
            <div className="w-16 h-16 rounded-full bg-brand-purple-bg border-2 border-ink flex items-center justify-center mx-auto text-primary mb-6">
              <Linkedin className="w-8 h-8" />
            </div>
            <h1 className="font-clash text-2xl font-bold mb-2">Connect LinkedIn</h1>
            <p className="text-sm text-muted font-satoshi">
              Connect your LinkedIn account so we can send personalised connection requests to your leads.
            </p>
          </div>

          <LinkedInConnectPanel orderId={orderId} onSuccess={handleSuccess} />
        </div>
      </Container>
    </div>
  );
}
