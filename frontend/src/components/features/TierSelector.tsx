'use client';

import React from 'react';
import { Badge } from '@/components/ui/Badge';
import { Check } from 'lucide-react';

interface TierPricing {
  tier: number;
  label: string;
  amount_cents: number;
  currency: string;
  display_price: string;
}

interface TierSelectorProps {
  selected: 200 | 350 | 500;
  onSelect: (tier: 200 | 350 | 500) => void;
  pricing?: TierPricing[];
}

const tierDescriptions: Record<number, string> = {
  200: 'Great for testing the waters with a focused outreach',
  350: 'Recommended balance of reach and quality',
  500: 'Maximum coverage across your target market',
};

const fallbackTiers = [
  { value: 200 as const, name: 'Starter', desc: tierDescriptions[200], price: '$20', recommended: false },
  { value: 350 as const, name: 'Growth', desc: tierDescriptions[350], price: '$27', recommended: true },
  { value: 500 as const, name: 'Scale', desc: tierDescriptions[500], price: '$40', recommended: false },
];

export function TierSelector({ selected, onSelect, pricing }: TierSelectorProps) {
  const tiers = pricing
    ? pricing.map((p) => ({
          value: p.tier as 200 | 350 | 500,
          name: p.label,
          desc: tierDescriptions[p.tier] || '',
          price: p.display_price,
          recommended: p.tier === 350,
        }))
    : fallbackTiers;

  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
      {tiers.map((tier) => {
        const isSelected = selected === tier.value;
        return (
          <button
            key={tier.value}
            onClick={() => onSelect(tier.value)}
            className={`relative flex flex-col items-center p-6 rounded-2xl border-2 transition-all duration-200
              ${isSelected
                ? 'border-ink bg-brand-purple-bg shadow-brutal'
                : 'border-ink/30 hover:border-ink hover:shadow-brutal-active'
              }`}
          >
            {tier.recommended && (
              <Badge variant="primary" className="absolute -top-3">Recommended</Badge>
            )}
            <span className="text-3xl text-primary font-bold font-clash">{tier.value}</span>
            <span className="text-lg font-bold text-ink mt-2 font-clash">{tier.name}</span>
            <p className="text-sm text-muted mt-2 text-center font-satoshi">{tier.desc}</p>
            <span className="text-base font-bold text-primary mt-3 font-clash">{tier.price}</span>
            {isSelected && (
              <div className="mt-3 w-8 h-8 rounded-full bg-primary border-2 border-ink flex items-center justify-center">
                <Check className="w-5 h-5 text-white" />
              </div>
            )}
          </button>
        );
      })}
    </div>
  );
}
