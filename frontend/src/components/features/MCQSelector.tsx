'use client';

import React, { useState } from 'react';
import { Button } from '@/components/ui/Button';
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

export function MCQSelector({ question, options, allowMultiple, onSubmit, loading }: MCQSelectorProps) {
  const [selected, setSelected] = useState<string[]>([]);

  const toggle = (label: string) => {
    if (allowMultiple) {
      setSelected((prev) =>
        prev.includes(label) ? prev.filter((l) => l !== label) : [...prev, label]
      );
    } else {
      setSelected([label]);
    }
  };

  const handleSubmit = () => {
    if (selected.length > 0) {
      const answers = selected.map((label) => {
        const opt = options.find((o) => o.label === label);
        return opt ? opt.text : label;
      });
      onSubmit(answers);
      setSelected([]);
    }
  };

  return (
    <div className="space-y-2.5">
      <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
        {options.map((opt) => {
          const isSelected = selected.includes(opt.label);
          return (
            <button
              key={opt.label}
              onClick={() => toggle(opt.label)}
              className={`flex items-center gap-2 px-3 py-2 rounded-xl text-left text-[13px] font-satoshi border-2 transition-all duration-150
                ${isSelected
                  ? 'border-primary bg-purple-50 text-primary shadow-sm'
                  : 'border-ink/20 text-ink hover:border-ink/40 hover:bg-surface-muted'
                }`}
            >
              <span className={`w-5 h-5 rounded-md flex items-center justify-center flex-shrink-0 text-[11px] font-semibold ${
                isSelected ? 'bg-primary text-white' : 'bg-surface-muted text-muted'
              }`}>
                {isSelected ? <Check className="w-3 h-3" /> : opt.label}
              </span>
              <span className="leading-tight">{opt.text}</span>
            </button>
          );
        })}
      </div>
      <div className="flex items-center gap-3">
        <Button size="sm" onClick={handleSubmit} loading={loading} disabled={selected.length === 0}>
          Continue
        </Button>
        {allowMultiple && (
          <span className="text-xs text-muted font-satoshi">Select multiple</span>
        )}
      </div>
    </div>
  );
}
