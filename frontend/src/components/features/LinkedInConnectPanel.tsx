'use client';

import { useState, useEffect } from 'react';
import { Eye, EyeOff, ShieldCheck, AlertCircle, Linkedin, ExternalLink } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Spinner } from '@/components/ui/Spinner';
import api from '@/lib/api';

interface Props {
  orderId: number;
  onSuccess: (campaignId: number) => void;
}

type LoginTab = 'password' | 'cookies' | 'extension';

export function LinkedInConnectPanel({ orderId, onSuccess }: Props) {
  // Password tab
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPass, setShowPass] = useState(false);
  const [loading, setLoading] = useState(false);

  // Cookie tab
  const [liAt, setLiAt] = useState('');
  const [jsessionid, setJsessionid] = useState('');
  const [cookieLoading, setCookieLoading] = useState(false);

  // Extension tab
  const [extInstalled, setExtInstalled] = useState(false);
  const [extLoading, setExtLoading] = useState(false);

  // Challenge
  const [challenge, setChallenge] = useState<{ type: 'pin' | 'phone_tap'; sessionKey: string } | null>(null);
  const [pin, setPin] = useState('');
  const [pinLoading, setPinLoading] = useState(false);
  const [phoneTapLoading, setPhoneTapLoading] = useState(false);

  const [tab, setTab] = useState<LoginTab>('password');
  const [error, setError] = useState('');
  const [creatingCampaign, setCreatingCampaign] = useState(false);

  // Detect extension
  useEffect(() => {
    const onReady = () => setExtInstalled(true);
    window.addEventListener('STUDOJO_EXT_READY', onReady);
    window.dispatchEvent(new CustomEvent('STUDOJO_CHECK_EXT'));
    return () => window.removeEventListener('STUDOJO_EXT_READY', onReady);
  }, []);

  const afterLogin = async () => {
    setCreatingCampaign(true);
    setError('');
    try {
      const res = await api.post('/linkedin/automation/campaigns/from-order', {
        order_id: orderId,
        daily_limit: 10,
      });
      onSuccess(res.data.id);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Could not create LinkedIn campaign. Please try again.');
    } finally {
      setCreatingCampaign(false);
    }
  };

  const handleConnect = async () => {
    setLoading(true);
    setError('');
    try {
      const res = await api.post('/linkedin/automation/login', { email, password });
      if (res.data.challenge_required) {
        setChallenge({
          type: res.data.challenge_type === 'phone_tap' ? 'phone_tap' : 'pin',
          sessionKey: res.data.session_key,
        });
        return;
      }
      await afterLogin();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Connection failed. Check your credentials.');
    } finally {
      setLoading(false);
    }
  };

  const handleVerifyPin = async () => {
    if (!challenge) return;
    setPinLoading(true);
    setError('');
    try {
      await api.post('/linkedin/automation/login/verify-pin', { session_key: challenge.sessionKey, pin });
      setChallenge(null);
      await afterLogin();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Incorrect code. Please try again.');
    } finally {
      setPinLoading(false);
    }
  };

  const handleCheckPhoneTap = async () => {
    if (!challenge) return;
    setPhoneTapLoading(true);
    setError('');
    try {
      const res = await api.post('/linkedin/automation/login/check-phone-tap', { session_key: challenge.sessionKey });
      if (res.data.still_waiting) {
        setError('Not approved yet — tap "Yes" on your phone first, then click Continue.');
        return;
      }
      setChallenge(null);
      await afterLogin();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Session expired. Please log in again.');
    } finally {
      setPhoneTapLoading(false);
    }
  };

  const handleCookieLogin = async () => {
    setCookieLoading(true);
    setError('');
    try {
      await api.post('/linkedin/automation/login/cookies', {
        li_at: liAt.trim(),
        jsessionid: jsessionid.trim(),
      });
      await afterLogin();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Invalid cookies. Please check and try again.');
    } finally {
      setCookieLoading(false);
    }
  };

  const handleExtensionLogin = () => {
    setExtLoading(true);
    setError('');
    let timeoutId: ReturnType<typeof setTimeout>;
    const onCookies = (e: Event) => {
      clearTimeout(timeoutId);
      const { li_at, jsessionid: jsid, cookies, error: extErr } = (e as CustomEvent).detail || {};
      window.removeEventListener('STUDOJO_LI_COOKIES', onCookies);
      if (extErr || !li_at) {
        setError(extErr || 'Extension could not read LinkedIn cookies. Make sure you\'re logged in to LinkedIn.');
        setExtLoading(false);
        return;
      }
      api.post('/linkedin/automation/login/cookies', { li_at, jsessionid: jsid || '', is_extension: true, cookies })
        .then(() => afterLogin())
        .catch((err: any) => {
          setError(err.response?.data?.detail || 'LinkedIn session invalid. Please re-login to LinkedIn and try again.');
        })
        .finally(() => setExtLoading(false));
    };
    window.addEventListener('STUDOJO_LI_COOKIES', onCookies);
    window.dispatchEvent(new CustomEvent('STUDOJO_REQUEST_LI_COOKIES'));
    timeoutId = setTimeout(() => {
      window.removeEventListener('STUDOJO_LI_COOKIES', onCookies);
      setExtLoading((prev) => {
        if (prev) setError('Extension did not respond. Try refreshing or installing the extension.');
        return false;
      });
    }, 5000);
  };

  if (creatingCampaign) {
    return (
      <div className="flex flex-col items-center gap-4 py-12">
        <Spinner />
        <p className="text-sm text-muted font-satoshi">Setting up your LinkedIn campaign...</p>
      </div>
    );
  }

  // ── Challenge screens ──────────────────────────────────────────────────────
  if (challenge?.type === 'pin') {
    return (
      <div className="space-y-4">
        <div className="flex items-start gap-3 p-4 rounded-xl bg-amber-50 border-2 border-amber-200">
          <AlertCircle className="w-5 h-5 text-amber-600 flex-shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-bold font-satoshi text-amber-800">Verification required</p>
            <p className="text-sm text-amber-700 font-satoshi mt-0.5">LinkedIn sent a verification code to your email. Enter it below.</p>
          </div>
        </div>
        <input
          type="text"
          inputMode="numeric"
          value={pin}
          onChange={(e) => setPin(e.target.value.replace(/\D/g, ''))}
          placeholder="6-digit code"
          maxLength={8}
          className="w-full border-2 border-ink rounded-xl px-4 py-3 text-center text-2xl font-bold tracking-widest font-clash"
          onKeyDown={(e) => e.key === 'Enter' && handleVerifyPin()}
        />
        {error && <p className="text-error text-sm font-satoshi">{error}</p>}
        <Button onClick={handleVerifyPin} loading={pinLoading} className="w-full">Verify Code</Button>
        <button onClick={() => setChallenge(null)} className="text-xs text-muted font-satoshi w-full text-center mt-2">← Use different credentials</button>
      </div>
    );
  }

  if (challenge?.type === 'phone_tap') {
    return (
      <div className="space-y-4">
        <div className="flex items-start gap-3 p-4 rounded-xl bg-blue-50 border-2 border-blue-200">
          <AlertCircle className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-bold font-satoshi text-blue-800">Approve on your phone</p>
            <p className="text-sm text-blue-700 font-satoshi mt-0.5">LinkedIn sent a push notification to your phone. Tap "Yes, it's me" then click Continue below.</p>
          </div>
        </div>
        {error && <p className="text-error text-sm font-satoshi">{error}</p>}
        <Button onClick={handleCheckPhoneTap} loading={phoneTapLoading} className="w-full">I approved it — Continue</Button>
        <button onClick={() => setChallenge(null)} className="text-xs text-muted font-satoshi w-full text-center mt-2">← Use different credentials</button>
      </div>
    );
  }

  // ── Main connect UI ────────────────────────────────────────────────────────
  return (
    <div className="space-y-6">
      {/* Privacy note */}
      <div className="flex items-start gap-3 p-4 rounded-xl bg-studojo-green-bg border-2 border-ink/20">
        <ShieldCheck className="w-5 h-5 text-secondary flex-shrink-0 mt-0.5" />
        <p className="text-sm text-muted font-satoshi">
          Your credentials are encrypted with AES-256-GCM and never stored in plaintext. We use them only to send connection requests on your behalf.
        </p>
      </div>

      {/* Tab switcher */}
      <div className="flex gap-2 rounded-xl border-2 border-ink/20 p-1 bg-surface-muted">
        {(['password', 'cookies', 'extension'] as const).map((t) => (
          <button
            key={t}
            onClick={() => { setTab(t); setError(''); }}
            className={`flex-1 py-2 rounded-lg text-xs font-bold font-satoshi transition-all ${
              tab === t ? 'bg-white border border-ink/10 shadow-sm text-primary' : 'text-muted'
            }`}
          >
            {t === 'password' ? 'Email & Password' : t === 'cookies' ? 'Paste Cookies' : 'Extension'}
          </button>
        ))}
      </div>

      {/* Password tab */}
      {tab === 'password' && (
        <div className="space-y-4">
          <input
            type="email"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="LinkedIn email"
            className="w-full border-2 border-ink rounded-xl px-4 py-3 text-sm font-satoshi"
          />
          <div className="relative">
            <input
              type={showPass ? 'text' : 'password'}
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="LinkedIn password"
              className="w-full border-2 border-ink rounded-xl px-4 py-3 text-sm font-satoshi pr-12"
              onKeyDown={(e) => e.key === 'Enter' && handleConnect()}
            />
            <button
              type="button"
              onClick={() => setShowPass(!showPass)}
              className="absolute right-4 top-1/2 -translate-y-1/2 text-muted"
            >
              {showPass ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
            </button>
          </div>
          {error && <p className="text-error text-sm font-satoshi">{error}</p>}
          <Button
            onClick={handleConnect}
            loading={loading}
            disabled={!email || !password}
            className="w-full"
          >
            <Linkedin className="w-4 h-4 mr-2 inline" /> Connect LinkedIn
          </Button>
        </div>
      )}

      {/* Cookie tab */}
      {tab === 'cookies' && (
        <div className="space-y-4">
          <p className="text-xs text-muted font-satoshi">
            Open LinkedIn in your browser → F12 → Application → Cookies → copy <code className="bg-surface-muted px-1 rounded">li_at</code> and <code className="bg-surface-muted px-1 rounded">JSESSIONID</code> values.
          </p>
          <input
            type="text"
            value={liAt}
            onChange={(e) => setLiAt(e.target.value)}
            placeholder="li_at cookie value"
            className="w-full border-2 border-ink rounded-xl px-4 py-3 text-sm font-satoshi font-mono"
          />
          <input
            type="text"
            value={jsessionid}
            onChange={(e) => setJsessionid(e.target.value)}
            placeholder="JSESSIONID cookie value"
            className="w-full border-2 border-ink rounded-xl px-4 py-3 text-sm font-satoshi font-mono"
          />
          {error && <p className="text-error text-sm font-satoshi">{error}</p>}
          <Button
            onClick={handleCookieLogin}
            loading={cookieLoading}
            disabled={!liAt || !jsessionid}
            className="w-full"
          >
            <Linkedin className="w-4 h-4 mr-2 inline" /> Connect with Cookies
          </Button>
        </div>
      )}

      {/* Extension tab */}
      {tab === 'extension' && (
        <div className="space-y-4">
          {!extInstalled ? (
            <div className="p-4 rounded-xl border-2 border-ink/20 bg-surface-muted space-y-3">
              <p className="text-sm font-satoshi text-muted">
                Install the Studojo browser extension, then come back here to connect with one click — no password needed.
              </p>
              <a
                href="https://chrome.google.com/webstore"
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-2 text-sm font-bold text-primary font-satoshi"
              >
                Install Extension <ExternalLink className="w-3.5 h-3.5" />
              </a>
            </div>
          ) : (
            <div className="flex items-center gap-3 p-3 rounded-xl bg-studojo-green-bg border-2 border-ink/20">
              <div className="w-2 h-2 rounded-full bg-secondary" />
              <p className="text-sm font-bold font-satoshi text-secondary">Studojo extension detected</p>
            </div>
          )}
          {error && <p className="text-error text-sm font-satoshi">{error}</p>}
          <Button
            onClick={handleExtensionLogin}
            loading={extLoading}
            disabled={!extInstalled}
            className="w-full"
          >
            <Linkedin className="w-4 h-4 mr-2 inline" /> Connect via Extension
          </Button>
        </div>
      )}
    </div>
  );
}
