'use client';

import React, { useRef, useEffect } from 'react';
import { Bot, UserIcon } from 'lucide-react';
interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
}

interface ChatInterfaceProps {
  messages: ChatMessage[];
  children?: React.ReactNode;
  loading?: boolean;
}

export function ChatInterface({ messages, children, loading }: ChatInterfaceProps) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, loading]);

  return (
    <div className="flex flex-col h-[calc(100vh-260px)] min-h-[480px] bg-white border border-gray-200 rounded-2xl overflow-hidden shadow-sm">
      {/* Chat messages — primary area, takes ~70% */}
      <div className="flex-1 overflow-y-auto px-4 py-5 space-y-4">
        {messages.map((msg, i) => {
          const parts = msg.role === 'assistant' ? msg.content.split('|||') : [msg.content];

          return (
            <div
              key={i}
              className={`flex gap-2.5 animate-in fade-in slide-in-from-bottom-2 duration-300 ${
                msg.role === 'user' ? 'justify-end' : 'justify-start'
              }`}
            >
              {msg.role === 'assistant' && (
                <div className="w-8 h-8 rounded-full bg-gradient-to-br from-blue-500 to-indigo-600 flex items-center justify-center flex-shrink-0 mt-0.5 shadow-sm">
                  <Bot className="w-4 h-4 text-white" />
                </div>
              )}
              <div
                className={`max-w-[75%] rounded-2xl px-4 py-3 ${
                  msg.role === 'user'
                    ? 'bg-blue-600 text-white rounded-br-md'
                    : 'bg-gray-100 text-gray-800 rounded-bl-md'
                }`}
              >
                {parts.map((part, j) => (
                  <p key={j} className={`text-[14px] leading-relaxed ${j > 0 ? 'mt-2' : ''}`}>
                    {part.trim()}
                  </p>
                ))}
              </div>
              {msg.role === 'user' && (
                <div className="w-8 h-8 rounded-full bg-gray-700 flex items-center justify-center flex-shrink-0 mt-0.5 shadow-sm">
                  <UserIcon className="w-4 h-4 text-white" />
                </div>
              )}
            </div>
          );
        })}

        {/* Typing indicator */}
        {loading && (
          <div className="flex gap-2.5 justify-start animate-in fade-in duration-200">
            <div className="w-8 h-8 rounded-full bg-gradient-to-br from-blue-500 to-indigo-600 flex items-center justify-center flex-shrink-0 shadow-sm">
              <Bot className="w-4 h-4 text-white" />
            </div>
            <div className="bg-gray-100 rounded-2xl rounded-bl-md px-4 py-3 flex items-center gap-1.5">
              <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
              <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
              <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
            </div>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* Response input panel — bottom ~30%, compact */}
      {children && (
        <div className="border-t border-gray-200 bg-gray-50/80 px-4 py-3 flex-shrink-0">
          {children}
        </div>
      )}
    </div>
  );
}