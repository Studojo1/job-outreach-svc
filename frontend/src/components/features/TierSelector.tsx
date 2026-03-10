'use client';

import React from 'react';
import { Badge } from '@/components/ui/Badge';
import { Check } from 'lucide-react';

interface TierSelectorProps {
  selected: 5 | 200 | 350 | 500;
  onSelect: (tier: 5 | 200 | 350 | 500) => void;
}

const tiers = [
  { value: 5 as const, name: 'Test', desc: 'Test mode with 5 leads (development only)', price: 'Free', testing: true },
  { value: 200 as const, name: 'Starter', desc: 'Great for testing the waters with a focused outreach', price: 'Basic' },
  { value: 350 as const, name: 'Growth', desc: 'Recommended balance of reach and quality', price: 'Popular', recommended: true },
  { value: 500 as const, name: 'Scale', desc: 'Maximum coverage across your target market', price: 'Premium' },
];

export function TierSelector({ selected, onSelect }: TierSelectorProps) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-4 gap-m">
      {tiers.map((tier) => {
        const isSelected = selected === tier.value;
        const isTesting = (tier as any).testing;
        return (
          <button
            key={tier.value}
            onClick={() => onSelect(tier.value)}
            className={`relative flex flex-col items-center p-xl rounded-xl border-2 transition-all duration-200
              ${isTesting ? 'border-amber-300 bg-amber-50/50' : isSelected
                ? 'border-primary bg-primary/5 shadow-elevated'
                : 'border-border-light hover:border-gray-300 hover:-translate-y-0.5'
              }`}
          >
            {isTesting && (
              <Badge variant="warning" className="absolute -top-3">Testing</Badge>
            )}
            {tier.recommended && (
              <Badge variant="primary" className="absolute -top-3">Recommended</Badge>
            )}
            <span className="text-h2 text-primary font-bold">{tier.value}</span>
            <span className="text-h3 text-text-primary mt-s">{tier.name}</span>
            <p className="text-body-sm text-text-secondary mt-s text-center">{tier.desc}</p>
            {isSelected && (
              <div className="mt-m w-8 h-8 rounded-full bg-primary flex items-center justify-center">
                <Check className="w-5 h-5 text-white" />
              </div>
            )}
          </button>
        );
      })}
    </div>
  );
}
