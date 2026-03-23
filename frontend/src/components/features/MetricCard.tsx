'use client';

import React from 'react';
import { TrendingUp, TrendingDown } from 'lucide-react';

interface MetricCardProps {
  label: string;
  value: number | string;
  icon: React.ReactNode;
  trend?: 'up' | 'down';
  trendValue?: string;
}

export function MetricCard({ label, value, icon, trend, trendValue }: MetricCardProps) {
  return (
    <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-6">
      <div className="flex items-center justify-between mb-4">
        <span className="text-xs font-bold uppercase text-muted font-satoshi">{label}</span>
        <div className="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center text-primary">
          {icon}
        </div>
      </div>
      <div className="font-clash text-2xl font-bold text-ink">{value}</div>
      {trend && trendValue && (
        <div className={`flex items-center gap-1 mt-2 text-sm font-satoshi ${trend === 'up' ? 'text-secondary' : 'text-error'}`}>
          {trend === 'up' ? <TrendingUp className="w-4 h-4" /> : <TrendingDown className="w-4 h-4" />}
          <span>{trendValue}</span>
        </div>
      )}
    </div>
  );
}
