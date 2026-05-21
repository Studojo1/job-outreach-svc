'use client';

import React, { useState } from 'react';
import { Check } from 'lucide-react';

interface MCQOption {
  label: string;
  text: string;
}

interface MCQSelectorProps {
  question: string;
  options: MCQOption[];
  allowMultiple: boolean;
  onSubmit: (selected: string[]) => void;
  loading?: boolean;
}

export function MCQSelector({ options, allowMultiple, onSubmit, loading }: MCQSelectorProps) {
  const [selected, setSelected] = useState<string[]>([]);

  const toggle = (label: string) => {
    if (allowMultiple) {
      setSelected((prev) =>
        prev.includes(label) ? prev.filter((l) => l !== label) : [...prev, label]
      );
    } else {
      // Single-select submits immediately
      const opt = options.find((o) => o.label === label);
      onSubmit([opt ? opt.text : label]);
    }
  };

  const handleContinue = () => {
    if (selected.length === 0) return;
    const answers = selected.map((label) => {
      const opt = options.find((o) => o.label === label);
      return opt ? opt.text : label;
    });
    onSubmit(answers);
    setSelected([]);
  };

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2.5">
        {options.map((opt) => {
          const isSelected = selected.includes(opt.label);
          return (
            <button
              key={opt.label}
              onClick={() => toggle(opt.label)}
              disabled={loading}
              className={`flex items-center gap-3 px-3.5 py-3 rounded-xl text-left text-sm font-satoshi border-2 transition-all duration-150 disabled:opacity-50 disabled:pointer-events-none
                ${isSelected
                  ? 'border-primary bg-purple-50 text-primary shadow-sm'
                  : 'border-ink/15 text-ink hover:border-ink/30 hover:bg-surface-muted bg-white'
                }`}
            >
              <span className={`w-7 h-7 rounded-md flex items-center justify-center flex-shrink-0 text-xs font-bold ${
                isSelected ? 'bg-primary text-white' : 'bg-surface-muted text-muted'
              }`}>
                {isSelected && !allowMultiple ? <Check className="w-3.5 h-3.5" /> : opt.label}
              </span>
              <span className="leading-tight">{opt.text}</span>
            </button>
          );
        })}
      </div>
      {allowMultiple && (
        <div className="flex items-center gap-3 pt-1">
          <button
            onClick={handleContinue}
            disabled={selected.length === 0 || loading}
            className="h-10 px-5 rounded-xl bg-primary text-white text-sm font-satoshi font-medium border-2 border-ink shadow-brutal transition-all hover:translate-x-[2px] hover:translate-y-[2px] hover:shadow-none disabled:opacity-40 disabled:pointer-events-none"
          >
            Continue
          </button>
          <span className="text-xs text-muted font-satoshi">Select all that apply</span>
        </div>
      )}
    </div>
  );
}
