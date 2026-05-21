'use client';

import { useState, useEffect, useRef } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { useAppStore } from '@/store/useAppStore';
import { Navbar } from '@/components/layout/Navbar';
import { ChatInterface } from '@/components/features/ChatInterface';
import { MCQSelector } from '@/components/features/MCQSelector';
import { Button } from '@/components/ui/Button';
import { Send, Check } from 'lucide-react';
import api, { API_BASE } from '@/lib/api';
import type { ChatMessage, AgentResponse } from '@/lib/types/candidate';

/** Call the deterministic streaming chat endpoint (question_engine, no LLM per turn). */
async function callChatStream(
  candidateId: number,
  message: string,
  history: ChatMessage[],
): Promise<AgentResponse> {
  const token = typeof window !== 'undefined' ? localStorage.getItem('token') : null;
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;

  const res = await fetch(`${API_BASE}/candidate/${candidateId}/chat/stream`, {
    method: 'POST',
    headers,
    credentials: 'include',
    body: JSON.stringify({
      message,
      chat_history: history.map((m) => ({ role: m.role, content: m.content })),
    }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);

  // SSE format: "data: {...}\n\n" — endpoint emits a single 'complete' event then closes.
  const text = await res.text();
  const match = text.match(/^data: (.+)$/m);
  if (!match) throw new Error('No SSE data event in response');
  return JSON.parse(match[1]) as AgentResponse;
}

const STEPS = ['Upload Resume', 'AI Chat', 'Your Profile'];
const CURRENT_STEP = 2;

export default function ChatPage() {
  const router = useRouter();
  useAuth();
  const { candidateId, chatHistory, addChatMessage, clearChatHistory, setCurrentStep } = useAppStore();
  const [loading, setLoading] = useState(false);
  const [currentResponse, setCurrentResponse] = useState<AgentResponse | null>(null);
  const [textInput, setTextInput] = useState('');
  const [started, setStarted] = useState(false);
  const autoStarted = useRef(false);

  const sendMessage = async (content: string) => {
    if (!candidateId) return;

    const userMsg: ChatMessage = { role: 'user', content };
    addChatMessage(userMsg);
    setLoading(true);

    try {
      const response = await callChatStream(candidateId, content, [...chatHistory, userMsg]);
      const assistantMsg: ChatMessage = { role: 'assistant', content: response.message };
      addChatMessage(assistantMsg);
      setCurrentResponse(response);

      if (response.is_complete) {
        const fullHistory = [...chatHistory, userMsg, { role: 'assistant' as const, content: response.message }];
        try {
          setLoading(true);
          await api.post(`/candidate/${candidateId}/generate-payload`, {
            message: '__generate__',
            chat_history: fullHistory.map((m) => ({ role: m.role, content: m.content })),
          });
          await new Promise((r) => setTimeout(r, 1500));
          setCurrentStep(3);
          router.push('/onboarding/profile');
        } catch {
          addChatMessage({ role: 'assistant', content: 'Profile generation failed. Please try again.' });
          setLoading(false);
        }
      }
    } catch {
      addChatMessage({ role: 'assistant', content: 'Something went wrong. Please try again.' });
    } finally {
      setLoading(false);
    }
  };

  // Boot / resume the quiz on every page load.
  //
  // chatHistory is persisted in localStorage so progress survives a refresh.
  // On resume (chatHistory has user answers), replay the history through the
  // question_engine to get the current question state back. The engine is
  // deterministic so replay is safe and instant.
  //
  // Three cases:
  //  1. Fresh start: chatHistory empty → __start__, add Q1 message.
  //  2. Mid-quiz, last msg is assistant: already have current question text +
  //     need MCQ options restored → replay with existing history.
  //  3. Mid-quiz, last msg is user: Q next hasn't been delivered yet →
  //     replay; engine returns next question and we add it to history.
  useEffect(() => {
    if (!candidateId || autoStarted.current) return;
    autoStarted.current = true;
    setStarted(true);
    setLoading(true);

    const hasUserAnswers = chatHistory.some((m) => m.role === 'user');

    if (!hasUserAnswers) {
      // Case 1 (and case where user refreshed before answering Q1):
      // Start clean — clear any stale Q1 bubble and restart.
      clearChatHistory();
      callChatStream(candidateId, '__start__', [])
        .then((response) => {
          addChatMessage({ role: 'assistant', content: response.message });
          setCurrentResponse(response);
        })
        .catch(() => {
          addChatMessage({ role: 'assistant', content: 'Failed to start chat. Please refresh.' });
        })
        .finally(() => setLoading(false));
    } else {
      // Cases 2 & 3: replay history to restore current question state.
      callChatStream(candidateId, '', chatHistory)
        .then((response) => {
          // If the last persisted message is from the user, the assistant
          // response for the current question hasn't been stored yet — add it.
          const lastMsg = chatHistory[chatHistory.length - 1];
          if (lastMsg?.role === 'user') {
            addChatMessage({ role: 'assistant', content: response.message });
          }
          setCurrentResponse(response);
        })
        .catch(() => {
          // Replay failed (e.g. stale history from old endpoint) — restart.
          clearChatHistory();
          callChatStream(candidateId, '__start__', [])
            .then((r) => {
              addChatMessage({ role: 'assistant', content: r.message });
              setCurrentResponse(r);
            })
            .finally(() => setLoading(false));
        })
        .finally(() => setLoading(false));
    }
  }, [candidateId]);

  const handleMCQSubmit = (selected: string[]) => {
    sendMessage(selected.join(', '));
  };

  const handleTextSubmit = () => {
    if (textInput.trim()) {
      sendMessage(textInput.trim());
      setTextInput('');
    }
  };

  if (!candidateId) {
    return (
      <div className="min-h-screen bg-white">
        <Navbar />
        <div className="mx-auto max-w-3xl px-4 py-8 md:px-8 text-center">
          <p className="text-base text-muted mt-8 font-satoshi">
            Please upload your resume first.
          </p>
          <Button onClick={() => router.push('/onboarding/upload')} className="mt-6">
            Go to Upload
          </Button>
        </div>
      </div>
    );
  }

  const questionsAsked = currentResponse?.questions_asked_so_far ?? 0;

  // MCQ / text input rendered in the bottom panel
  const bottomPanel = currentResponse?.is_complete ? null
    : currentResponse?.mcq ? (
      <MCQSelector
        question={currentResponse.mcq.question}
        options={currentResponse.mcq.options}
        allowMultiple={currentResponse.mcq.allow_multiple}
        onSubmit={handleMCQSubmit}
        loading={loading}
      />
    ) : (currentResponse?.text_input || (!currentResponse?.mcq && started && !loading)) ? (
      <div className="flex gap-2 items-end">
        <textarea
          value={textInput}
          onChange={(e) => setTextInput(e.target.value)}
          placeholder={(currentResponse as { input_placeholder?: string } | null)?.input_placeholder || 'Type your answer...'}
          onKeyDown={(e) => e.key === 'Enter' && !e.shiftKey && (e.preventDefault(), handleTextSubmit())}
          rows={2}
          className="flex-1 px-4 py-2.5 rounded-xl border-2 border-ink/20 text-sm font-satoshi focus:outline-none focus:ring-2 focus:ring-primary resize-none"
        />
        <button
          onClick={handleTextSubmit}
          disabled={!textInput.trim() || loading}
          className="h-10 w-10 rounded-xl bg-primary text-white flex items-center justify-center border-2 border-ink shadow-brutal transition-all hover:translate-x-[2px] hover:translate-y-[2px] hover:shadow-none disabled:opacity-50 disabled:pointer-events-none flex-shrink-0"
        >
          <Send className="w-4 h-4" />
        </button>
      </div>
    ) : null;

  return (
    <div className="h-screen flex flex-col overflow-hidden bg-white">
      <Navbar />
      <div className="flex-1 flex overflow-hidden">
        {/* Sidebar — vertical stepper */}
        <aside className="hidden md:flex flex-col w-56 border-r border-ink/10 bg-surface-muted/30 items-center justify-center flex-shrink-0">
          <div className="flex flex-col" style={{ alignItems: 'flex-start' }}>
            {STEPS.map((label, idx) => {
              const n = idx + 1;
              const isDone = n < CURRENT_STEP;
              const isCurrent = n === CURRENT_STEP;
              const isLast = idx === STEPS.length - 1;
              return (
                <div key={idx} className="flex flex-col items-start">
                  <div className="flex items-center gap-3">
                    <div className="flex flex-col items-center">
                      <div
                        className={`w-9 h-9 rounded-full flex items-center justify-center text-sm font-bold border-2 transition-all duration-300 ${
                          isDone
                            ? 'bg-secondary text-white border-secondary'
                            : isCurrent
                            ? 'bg-primary text-white border-primary'
                            : 'bg-white text-muted border-ink/15'
                        }`}
                      >
                        {isDone ? <Check className="w-4 h-4" /> : n}
                      </div>
                      {!isLast && (
                        <div className={`w-0.5 h-10 ${isDone ? 'bg-secondary' : 'bg-ink/10'}`} />
                      )}
                    </div>
                    <div>
                      <p
                        className={`text-sm font-satoshi leading-tight ${
                          isCurrent ? 'text-ink font-semibold'
                            : isDone ? 'text-secondary font-medium'
                            : 'text-muted'
                        }`}
                      >
                        {label}
                      </p>
                      {isCurrent && n === 2 && questionsAsked > 0 && (
                        <p className="text-xs text-muted font-satoshi mt-0.5">Q {questionsAsked}</p>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </aside>

        {/* Main content */}
        <div className="flex-1 flex flex-col overflow-hidden">
          {/* Mobile compact stepper */}
          <div className="md:hidden flex items-center justify-between px-4 pt-4 pb-1 flex-shrink-0">
            <div className="flex items-center gap-1.5">
              {STEPS.map((_, idx) => (
                <div
                  key={idx}
                  className={`w-2 h-2 rounded-full transition-colors ${
                    idx + 1 < CURRENT_STEP ? 'bg-secondary'
                      : idx + 1 === CURRENT_STEP ? 'bg-primary'
                      : 'bg-ink/15'
                  }`}
                />
              ))}
            </div>
            {questionsAsked > 0 && (
              <span className="text-xs font-satoshi text-muted">Q{questionsAsked}</span>
            )}
          </div>

          {/* Heading */}
          <div className="flex-shrink-0 px-6 pt-6 md:pt-8 pb-2">
            <h1 className="font-clash text-xl md:text-2xl font-bold text-ink">Quick Profile Setup</h1>
            <p className="text-sm text-muted font-satoshi mt-1">
              A few quick questions so we can find the right hiring managers for you.
            </p>
          </div>

          {/* Chat + bottom panel */}
          <div className="flex-1 overflow-hidden px-4 md:px-6 pb-4">
            <ChatInterface messages={chatHistory} loading={loading}>
              {currentResponse?.is_complete ? (
                <div className="text-center p-4">
                  <p className="text-sm text-secondary font-semibold font-satoshi">
                    Profile complete! Redirecting...
                  </p>
                </div>
              ) : bottomPanel}
            </ChatInterface>
          </div>
        </div>
      </div>
    </div>
  );
}
