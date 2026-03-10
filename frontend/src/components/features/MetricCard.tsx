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
    <div className="bg-card border border-border-light rounded-xl shadow-soft p-l">
      <div className="flex items-center justify-between mb-m">
        <span className="text-label uppercase text-text-secondary">{label}</span>
        <div className="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center text-primary">
          {icon}
        </div>
      </div>
      <div className="text-h2 text-text-primary">{value}</div>
      {trend && trendValue && (
        <div className={`flex items-center gap-xs mt-s text-body-sm ${trend === 'up' ? 'text-secondary' : 'text-error'}`}>
          {trend === 'up' ? <TrendingUp className="w-4 h-4" /> : <TrendingDown className="w-4 h-4" />}
          <span>{trendValue}</span>
        </div>
      )}
    </div>
  );
}
