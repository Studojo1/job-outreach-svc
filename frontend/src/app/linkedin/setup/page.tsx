'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { Button } from '@/components/ui/Button';
import { MessageSquare, Zap, AlertCircle, ChevronDown, ChevronUp } from 'lucide-react';
import api from '@/lib/api';

const NOTE_PLACEHOLDER = `Hi {{name}}, I came across your profile — love what you're building at {{company}}. Would love to connect!`;
const FOLLOWUP_PLACEHOLDER = `Hey {{name}}, thanks for connecting! I'm a student deeply interested in {{role}} and the work you're doing at {{company}}. Would love to chat if you have 15 minutes.`;

export default function LinkedInSetupPage() {
  const router = useRouter();
  useAuth();

  const [campaignId, setCampaignId] = useState<number | null>(null);
  const [connectionNote, setConnectionNote] = useState('');
  const [followupMessage, setFollowupMessage] = useState('');
  const [dailyLimit, setDailyLimit] = useState(20);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [saving, setSaving] = useState(false);
  const [launching, setLaunching] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    const id = sessionStorage.getItem('li_campaign_id');
    if (!id) { router.push('/linkedin/quiz'); return; }
    setCampaignId(Number(id));
  }, []);

  const handleLaunch = async () => {
    if (!campaignId) return;
    setLaunching(true);
    setError('');

    try {
      // Save message templates first
      await api.put(`/linkedin/automation/campaigns/${campaignId}`, {
        ...(await api.get(`/linkedin/automation/campaigns/${campaignId}`)).data,
        connection_note: connectionNote || NOTE_PLACEHOLDER,
        followup_message: followupMessage || FOLLOWUP_PLACEHOLDER,
        daily_limit: dailyLimit,
      });

      // Launch
      await api.post(`/linkedin/automation/campaigns/${campaignId}/launch`);

      // Clear session storage and go to dashboard
      sessionStorage.removeItem('li_quiz');
      sessionStorage.removeItem('li_campaign_id');
      router.push(`/linkedin/dashboard?id=${campaignId}`);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Launch failed. Please try again.');
      setLaunching(false);
    }
  };

  const noteLength = connectionNote.length;
  const noteOver = noteLength > 300;

  return (
    <div className="min-h-screen bg-background">
      <Navbar />
      <Container className="py-10 max-w-xl">
        <div className="mb-8">
          <h1 className="text-2xl font-bold mb-1">Set up your messages</h1>
          <p className="text-muted-foreground text-sm">
            We personalise these for each lead using their name, company, and headline.
          </p>
        </div>

        <div className="space-y-6">
          {/* Connection note */}
          <div className="bg-white border border-border rounded-2xl p-5">
            <div className="flex items-center gap-2 mb-3">
              <MessageSquare className="w-4 h-4 text-primary" />
              <h3 className="font-semibold text-sm">Connection note</h3>
              <span className="text-xs text-muted-foreground ml-auto">max 300 chars</span>
            </div>
            <textarea
              value={connectionNote}
              onChange={e => setConnectionNote(e.target.value)}
              placeholder={NOTE_PLACEHOLDER}
              rows={3}
              className={`w-full text-sm border rounded-xl px-3 py-2.5 resize-none focus:outline-none focus:ring-2 focus:ring-primary/30 ${
                noteOver ? 'border-red-300' : 'border-border'
              }`}
            />
            <div className="flex justify-between items-center mt-1.5">
              <p className="text-xs text-muted-foreground">
                Use <code className="bg-gray-100 px-1 rounded">{'{{name}}'}</code> and <code className="bg-gray-100 px-1 rounded">{'{{company}}'}</code> — AI fills these in per lead
              </p>
              <span className={`text-xs ${noteOver ? 'text-red-500' : 'text-muted-foreground'}`}>
                {noteLength}/300
              </span>
            </div>
          </div>

          {/* Follow-up message */}
          <div className="bg-white border border-border rounded-2xl p-5">
            <div className="flex items-center gap-2 mb-3">
              <Zap className="w-4 h-4 text-amber-500" />
              <h3 className="font-semibold text-sm">Follow-up message</h3>
              <span className="text-xs text-muted-foreground ml-1">sent after they accept</span>
            </div>
            <textarea
              value={followupMessage}
              onChange={e => setFollowupMessage(e.target.value)}
              placeholder={FOLLOWUP_PLACEHOLDER}
              rows={4}
              className="w-full text-sm border border-border rounded-xl px-3 py-2.5 resize-none focus:outline-none focus:ring-2 focus:ring-primary/30"
            />
            <p className="text-xs text-muted-foreground mt-1.5">
              Use <code className="bg-gray-100 px-1 rounded">{'{{name}}'}</code>, <code className="bg-gray-100 px-1 rounded">{'{{company}}'}</code>, <code className="bg-gray-100 px-1 rounded">{'{{role}}'}</code>
            </p>
          </div>

          {/* Advanced */}
          <button
            onClick={() => setShowAdvanced(v => !v)}
            className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
          >
            {showAdvanced ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
            Advanced settings
          </button>

          {showAdvanced && (
            <div className="bg-white border border-border rounded-2xl p-5">
              <label className="text-sm font-medium block mb-2">Daily connection limit</label>
              <div className="flex items-center gap-4">
                <input
                  type="range"
                  min={5}
                  max={40}
                  value={dailyLimit}
                  onChange={e => setDailyLimit(Number(e.target.value))}
                  className="flex-1"
                />
                <span className="text-sm font-semibold w-16 text-right">{dailyLimit}/day</span>
              </div>
              <p className="text-xs text-muted-foreground mt-1.5">
                We recommend 15–25/day. LinkedIn's soft limit is ~100/week.
              </p>
            </div>
          )}

          {error && (
            <div className="flex items-start gap-2 bg-red-50 border border-red-200 rounded-xl p-3 text-sm text-red-700">
              <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
              <span>{error}</span>
            </div>
          )}

          <Button
            onClick={handleLaunch}
            disabled={noteOver || launching}
            className="w-full"
          >
            {launching ? 'Launching...' : 'Launch campaign'}
          </Button>
        </div>
      </Container>
    </div>
  );
}
