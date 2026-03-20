'use client';

import { Suspense, useEffect, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { useAppStore } from '@/store/useAppStore';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { Button } from '@/components/ui/Button';
import { Spinner } from '@/components/ui/Spinner';
import { API_BASE } from '@/lib/api';
import api from '@/lib/api';
import { useOrder } from '@/lib/hooks/useOrder';
import { Mail, Shield, Eye, Send, CheckCircle } from 'lucide-react';

function GmailConnectContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { loading } = useAuth();
  const { emailAccountId, setEmailAccountId } = useAppStore();
  const { updateOrder } = useOrder();
  const [connected, setConnected] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState('');
  const [handled, setHandled] = useState(false);

  useEffect(() => {
    if (handled) return;
    const status = searchParams.get('status');
    const errorMsg = searchParams.get('message');

    if (status === 'error') {
      setError(errorMsg || 'Gmail connection failed. Please try again.');
      setHandled(true);
      return;
    }

    if (status === 'success') {
      setHandled(true);
      setConnecting(true);
      api.get('/gmail/oauth/account')
        .then((res) => {
          const accountId = res.data?.email_account_id;
          if (accountId) {
            setEmailAccountId(accountId);
            updateOrder({ status: 'email_connected', email_account_id: accountId, log_entry: 'Gmail account connected' });
          }
          setConnected(true);
        })
        .catch(() => {
          setConnected(true);
        })
        .finally(() => setConnecting(false));
    } else if (emailAccountId && !connected) {
      setHandled(true);
      setConnecting(true);
      api.get('/gmail/oauth/account')
        .then((res) => {
          if (res.data?.email_account_id) {
            setConnected(true);
          }
        })
        .catch(() => {
          setEmailAccountId(0);
        })
        .finally(() => setConnecting(false));
    }
  }, [searchParams, handled]);

  const handleConnect = () => { window.location.href = `${API_BASE}/gmail/oauth/connect`; };

  const handleContinue = () => {
    router.push('/campaign/templates');
  };

  const permissions = [
    { icon: <Send className="w-5 h-5" />, label: 'Send Emails', desc: 'Send personalized outreach on your behalf' },
    { icon: <Eye className="w-5 h-5" />, label: 'Read Replies', desc: 'Track when leads respond to your emails' },
    { icon: <Shield className="w-5 h-5" />, label: 'Your Email Address', desc: 'Identify which Gmail account to use' },
  ];

  if (loading || connecting) return <div className="min-h-screen flex items-center justify-center bg-white"><Spinner /></div>;

  return (
    <div className="min-h-screen bg-white">
      <Navbar />
      <Container className="max-w-onboarding py-8">
        {connected ? (
          <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-8 text-center animate-fade-in">
            <div className="w-16 h-16 rounded-full bg-studojo-green-bg border-2 border-ink flex items-center justify-center mx-auto mb-6">
              <CheckCircle className="w-8 h-8 text-secondary" />
            </div>
            <h1 className="font-clash text-2xl font-bold mb-2">Gmail Connected</h1>
            <p className="text-base text-muted mb-8 font-satoshi">Your Gmail account is ready to send outreach emails.</p>
            <Button onClick={handleContinue}>Continue to Campaign Setup</Button>
          </div>
        ) : (
          <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-8">
            <div className="text-center mb-10">
              <div className="w-16 h-16 rounded-full bg-brand-purple-bg border-2 border-ink flex items-center justify-center mx-auto text-primary mb-6">
                <Mail className="w-8 h-8" />
              </div>
              <h1 className="font-clash text-2xl font-bold mb-2">Connect Your Gmail</h1>
              <p className="text-sm text-muted font-satoshi">We need Gmail access to send outreach emails from your account.</p>
            </div>

            <div className="space-y-3 mb-8">
              <h3 className="font-clash text-lg font-bold">Permissions Required</h3>
              {permissions.map((p, i) => (
                <div key={i} className="flex items-start gap-4 p-4 bg-surface-muted rounded-xl border-2 border-ink/20">
                  <div className="w-10 h-10 rounded-xl bg-brand-purple-bg border-2 border-ink flex items-center justify-center text-primary flex-shrink-0">
                    {p.icon}
                  </div>
                  <div>
                    <p className="text-sm font-bold font-satoshi">{p.label}</p>
                    <p className="text-sm text-muted font-satoshi">{p.desc}</p>
                  </div>
                </div>
              ))}
            </div>

            <div className="bg-studojo-green-bg rounded-xl border-2 border-ink/20 p-4 mb-8">
              <div className="flex items-center gap-2 mb-2">
                <Shield className="w-4 h-4 text-secondary" />
                <span className="text-sm font-bold text-secondary font-satoshi">Your data is safe</span>
              </div>
              <p className="text-sm text-muted font-satoshi">
                We never store your emails. Access is used solely for sending your approved outreach messages and tracking replies.
              </p>
            </div>

            {error && (
              <div className="bg-red-50 rounded-xl border-2 border-red-200 p-4 mb-8">
                <p className="text-sm text-red-700 font-satoshi font-bold mb-1">Connection failed</p>
                <p className="text-sm text-red-600 font-satoshi">{error}</p>
              </div>
            )}

            <Button size="lg" onClick={handleConnect} className="w-full">Connect Gmail Account</Button>
          </div>
        )}
      </Container>
    </div>
  );
}

export default function GmailConnectPage() {
  return (
    <Suspense fallback={<div className="min-h-screen flex items-center justify-center bg-white"><Spinner /></div>}>
      <GmailConnectContent />
    </Suspense>
  );
}
