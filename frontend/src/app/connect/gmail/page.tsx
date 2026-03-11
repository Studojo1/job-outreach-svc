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
import { Mail, Shield, Eye, Send, CheckCircle } from 'lucide-react';

function GmailConnectContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  useAuth();
  const { setEmailAccountId } = useAppStore();
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    const status = searchParams.get('status');
    if (status === 'success') {
      setConnected(true);
      setEmailAccountId(1);
    }
  }, [searchParams, setEmailAccountId]);

  const handleConnect = () => {
    const token = typeof window !== 'undefined' ? localStorage.getItem('token') : null;
    if (!token) {
      alert('Please log in first.');
      return;
    }
    window.location.href = `${API_BASE}/gmail/oauth/connect?token=${encodeURIComponent(token)}`;
  };

  const permissions = [
    { icon: <Send className="w-5 h-5" />, label: 'Send Emails', desc: 'Send personalized outreach on your behalf' },
    { icon: <Eye className="w-5 h-5" />, label: 'Read Replies', desc: 'Track when leads respond to your emails' },
    { icon: <Shield className="w-5 h-5" />, label: 'Your Email Address', desc: 'Identify which Gmail account to use' },
  ];

  return (
    <div className="min-h-screen bg-page">
      <Navbar />
      <Container className="max-w-onboarding py-xl">
        {connected ? (
          <div className="card p-xl text-center animate-fade-in">
            <CheckCircle className="w-16 h-16 text-secondary mx-auto mb-l" />
            <h1 className="text-h2 mb-s">Gmail Connected</h1>
            <p className="text-body-lg text-text-secondary mb-xl">
              Your Gmail account is ready to send outreach emails.
            </p>
            <Button onClick={() => router.push('/campaign/templates')}>
              Choose Email Template
            </Button>
          </div>
        ) : (
          <div className="card p-xl">
            <div className="text-center mb-xl">
              <div className="w-16 h-16 rounded-full bg-primary/10 flex items-center justify-center mx-auto text-primary mb-l">
                <Mail className="w-8 h-8" />
              </div>
              <h1 className="text-h2 mb-s">Connect Your Gmail</h1>
              <p className="text-body-sm text-text-secondary">
                We need Gmail access to send outreach emails from your account.
              </p>
            </div>

            <div className="space-y-m mb-xl">
              <h3 className="text-h3">Permissions Required</h3>
              {permissions.map((p, i) => (
                <div key={i} className="flex items-start gap-m p-m bg-gray-50 rounded-lg">
                  <div className="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center text-primary flex-shrink-0">
                    {p.icon}
                  </div>
                  <div>
                    <p className="text-body-sm font-semibold">{p.label}</p>
                    <p className="text-body-sm text-text-secondary">{p.desc}</p>
                  </div>
                </div>
              ))}
            </div>

            <div className="bg-gray-50 rounded-lg p-m mb-xl">
              <div className="flex items-center gap-s mb-s">
                <Shield className="w-4 h-4 text-secondary" />
                <span className="text-body-sm font-semibold text-secondary">Your data is safe</span>
              </div>
              <p className="text-body-sm text-text-secondary">
                We never store your emails. Access is used solely for sending your approved outreach messages and tracking replies.
              </p>
            </div>

            <Button size="lg" onClick={handleConnect} className="w-full">
              Connect Gmail Account
            </Button>
          </div>
        )}
      </Container>
    </div>
  );
}

export default function GmailConnectPage() {
  return (
    <Suspense fallback={
      <div className="min-h-screen flex items-center justify-center bg-page">
        <Spinner />
      </div>
    }>
      <GmailConnectContent />
    </Suspense>
  );
}