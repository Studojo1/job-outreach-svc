'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { Button } from '@/components/ui/Button';
import { Linkedin, Lock, Eye, EyeOff, ShieldCheck, AlertCircle } from 'lucide-react';
import api from '@/lib/api';

export default function LinkedInConnectPage() {
  const router = useRouter();
  useAuth();

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPass, setShowPass] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleConnect = async () => {
    if (!email || !password) return;
    setLoading(true);
    setError('');

    try {
      await api.post('/linkedin/automation/login', { email, password });
      // Move to leads search step
      router.push('/linkedin/leads');
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Connection failed. Check your credentials and try again.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-background">
      <Navbar />
      <Container className="py-10 max-w-md">
        <div className="text-center mb-8">
          <div className="w-14 h-14 bg-[#0077B5] rounded-2xl flex items-center justify-center mx-auto mb-4">
            <Linkedin className="w-7 h-7 text-white" />
          </div>
          <h1 className="text-2xl font-bold mb-2">Connect your LinkedIn</h1>
          <p className="text-muted-foreground text-sm">
            Your credentials are encrypted and never stored in plain text. We use them only to send connection requests on your behalf.
          </p>
        </div>

        <div className="bg-white border border-border rounded-2xl p-6 space-y-4">
          <div>
            <label className="text-sm font-medium text-foreground block mb-1.5">LinkedIn email</label>
            <input
              type="email"
              value={email}
              onChange={e => setEmail(e.target.value)}
              placeholder="you@example.com"
              className="w-full border border-border rounded-xl px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
              autoComplete="email"
            />
          </div>

          <div>
            <label className="text-sm font-medium text-foreground block mb-1.5">LinkedIn password</label>
            <div className="relative">
              <input
                type={showPass ? 'text' : 'password'}
                value={password}
                onChange={e => setPassword(e.target.value)}
                placeholder="••••••••"
                className="w-full border border-border rounded-xl px-4 py-3 pr-10 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
                autoComplete="current-password"
                onKeyDown={e => e.key === 'Enter' && handleConnect()}
              />
              <button
                type="button"
                onClick={() => setShowPass(v => !v)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
              >
                {showPass ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </div>
          </div>

          {error && (
            <div className="flex items-start gap-2 bg-red-50 border border-red-200 rounded-xl p-3 text-sm text-red-700">
              <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
              <span>{error}</span>
            </div>
          )}

          <Button
            onClick={handleConnect}
            disabled={!email || !password || loading}
            className="w-full"
          >
            {loading ? 'Connecting...' : 'Connect LinkedIn'}
          </Button>
        </div>

        <div className="mt-5 space-y-2.5">
          {[
            'Credentials encrypted with AES-256 before storage',
            'We never post, like, or share anything on your behalf',
            'You can disconnect at any time',
          ].map(item => (
            <div key={item} className="flex items-center gap-2.5 text-sm text-muted-foreground">
              <ShieldCheck className="w-4 h-4 text-green-600 flex-shrink-0" />
              <span>{item}</span>
            </div>
          ))}
        </div>

        <p className="text-center text-xs text-muted-foreground mt-6">
          If LinkedIn asks for verification, complete it in your browser first, then come back here.
        </p>
      </Container>
    </div>
  );
}
