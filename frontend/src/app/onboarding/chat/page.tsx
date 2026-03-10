'use client';

import { useState, useEffect, useRef } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { useAppStore } from '@/store/useAppStore';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { ProgressSteps } from '@/components/ui/ProgressSteps';
import { ChatInterface } from '@/components/features/ChatInterface';
import { MCQSelector } from '@/components/features/MCQSelector';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Send } from 'lucide-react';
import api from '@/lib/api';
import type { ChatMessage, AgentResponse } from '@/lib/types/candidate';

export default function ChatPage() {
  const router = useRouter();
  useAuth();
  const { candidateId, chatHistory, addChatMessage, setCurrentStep } = useAppStore();
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
      const res = await api.post(`/candidate/${candidateId}/chat`, {
        message: content,
        chat_history: [...chatHistory, userMsg].map((m) => ({
          role: m.role,
          content: m.content,
        })),
      });

      const response: AgentResponse = res.data;
      const assistantMsg: ChatMessage = { role: 'assistant', content: response.message };
      addChatMessage(assistantMsg);
      setCurrentResponse(response);

      // Debug logging for quiz state
      console.log('[QUIZ] Response received:', {
        state: response.current_state,
        has_mcq: !!response.mcq,
        mcq_options: response.mcq?.options?.length ?? 0,
        text_input: response.text_input,
        is_complete: response.is_complete,
        questions_asked: response.questions_asked_so_far,
      });
      console.log('[QUIZ] Full response:', response);

      if (response.is_complete) {
        // Generate the full candidate profile payload before navigating
        const fullHistory = [...chatHistory, userMsg, { role: 'assistant' as const, content: response.message }];
        try {
          setLoading(true);
          console.log('[ProfileGeneration] Starting profile generation...');
          const payloadRes = await api.post(`/candidate/${candidateId}/generate-payload`, {
            message: '__generate__',
            chat_history: fullHistory.map((m) => ({
              role: m.role,
              content: m.content,
            })),
          });
          console.log('[ProfileGeneration] Profile created successfully', payloadRes.data);
          // Brief delay so user sees "Profile complete" message
          await new Promise((r) => setTimeout(r, 1500));
          console.log('[ProfileGeneration] Redirecting to profile page');
          setCurrentStep(3);
          router.push('/onboarding/profile');
        } catch (err) {
          console.error('[ProfileGeneration] Failed to generate profile payload:', err);
          addChatMessage({
            role: 'assistant',
            content: 'Profile generation failed. Please try again.',
          });
          setLoading(false);
        }
      }
    } catch (err: any) {
      console.error('[QUIZ] Chat error:', err);
      addChatMessage({
        role: 'assistant',
        content: 'Something went wrong. Please try again.',
      });
    } finally {
      setLoading(false);
    }
  };

  const startChat = async () => {
    setStarted(true);
    setLoading(true);
    try {
      const res = await api.post(`/candidate/${candidateId}/chat`, {
        message: '__start__',
        chat_history: [],
      });
      const response: AgentResponse = res.data;
      addChatMessage({ role: 'assistant', content: response.message });
      setCurrentResponse(response);
    } catch {
      addChatMessage({ role: 'assistant', content: 'Failed to start chat. Please refresh.' });
    } finally {
      setLoading(false);
    }
  };

  // Auto-start the quiz immediately — no intermediate screen
  useEffect(() => {
    if (candidateId && !started && !autoStarted.current && chatHistory.length === 0) {
      autoStarted.current = true;
      startChat();
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
      <div className="min-h-screen bg-page">
        <Navbar />
        <Container className="max-w-onboarding py-xl text-center">
          <p className="text-body-lg text-text-secondary mt-xl">
            Please upload your resume first.
          </p>
          <Button onClick={() => router.push('/onboarding/upload')} className="mt-l">
            Go to Upload
          </Button>
        </Container>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-page">
      <Navbar />
      <Container className="max-w-onboarding py-xl">
        <ProgressSteps steps={['Upload Resume', 'AI Chat', 'Your Profile']} currentStep={2} />

        <div className="mt-xl">
          <h1 className="text-h2 mb-s">Career Intelligence Chat</h1>
          <p className="text-body-sm text-text-secondary mb-l">
            Our AI will ask you a few questions to understand your career goals.
          </p>

          <ChatInterface messages={chatHistory} loading={loading}>
            {currentResponse?.is_complete ? (
              <div className="text-center p-m">
                <p className="text-body-sm text-secondary font-semibold">
                  Profile complete! Redirecting...
                </p>
              </div>
            ) : currentResponse?.mcq ? (
              <MCQSelector
                question={currentResponse.mcq.question}
                options={currentResponse.mcq.options}
                allowMultiple={currentResponse.mcq.allow_multiple}
                onSubmit={handleMCQSubmit}
                loading={loading}
              />
            ) : currentResponse?.text_input || (!currentResponse?.mcq && started) ? (
              <div className="flex gap-s">
                <Input
                  value={textInput}
                  onChange={(e) => setTextInput(e.target.value)}
                  placeholder="Type your answer..."
                  onKeyDown={(e) => e.key === 'Enter' && handleTextSubmit()}
                  className="flex-1"
                />
                <Button onClick={handleTextSubmit} disabled={!textInput.trim() || loading}>
                  <Send className="w-4 h-4" />
                </Button>
              </div>
            ) : null}
          </ChatInterface>
        </div>
      </Container>
    </div>
  );
}