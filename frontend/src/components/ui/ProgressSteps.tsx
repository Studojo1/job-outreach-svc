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
            <div className="flex flex-col items-center gap-s">
              <div
                className={`w-10 h-10 rounded-full flex items-center justify-center text-body-sm font-bold transition-all duration-300
                  ${isCompleted ? 'bg-secondary text-white' : isActive ? 'bg-primary text-white' : 'border-2 border-border-light text-text-secondary'}`}
              >
                {isCompleted ? <Check className="w-5 h-5" /> : stepNum}
              </div>
              <span className={`text-body-sm whitespace-nowrap ${isActive ? 'text-primary font-semibold' : 'text-text-secondary'}`}>
                {step}
              </span>
            </div>
            {i < steps.length - 1 && (
              <div className={`w-16 h-0.5 mb-6 ${isCompleted ? 'bg-secondary' : 'bg-border-light'}`} />
            )}
          </React.Fragment>
        );
      })}
    </div>
  );
}
