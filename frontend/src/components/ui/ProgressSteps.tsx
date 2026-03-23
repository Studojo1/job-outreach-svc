'use client';

import React from 'react';
import { Check } from 'lucide-react';

interface ProgressStepsProps {
  steps: string[];
  currentStep: number;
}

export function ProgressSteps({ steps, currentStep }: ProgressStepsProps) {
  return (
    <div className="flex items-center justify-center gap-0">
      {steps.map((step, i) => {
        const stepNum = i + 1;
        const isCompleted = stepNum < currentStep;
        const isActive = stepNum === currentStep;

        return (
          <React.Fragment key={i}>
            <div className="flex flex-col items-center gap-2">
              <div
                className={`w-10 h-10 rounded-full flex items-center justify-center text-sm font-bold border-2 border-ink transition-all duration-300
                  ${isCompleted ? 'bg-secondary text-white' : isActive ? 'bg-primary text-white' : 'bg-white text-muted'}`}
              >
                {isCompleted ? <Check className="w-5 h-5" /> : stepNum}
              </div>
              <span className={`text-sm whitespace-nowrap font-satoshi ${isActive ? 'text-primary font-bold' : 'text-muted'}`}>
                {step}
              </span>
            </div>
            {i < steps.length - 1 && (
              <div className={`w-16 h-0.5 mb-6 ${isCompleted ? 'bg-secondary' : 'bg-neutral-300'}`} />
            )}
          </React.Fragment>
        );
      })}
    </div>
  );
}
