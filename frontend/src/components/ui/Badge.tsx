import React from 'react';

interface BadgeProps {
  children: React.ReactNode;
  variant?: 'default' | 'success' | 'warning' | 'error' | 'primary';
  className?: string;
}

export function Badge({ children, variant = 'default', className = '' }: BadgeProps) {
  const variants = {
    default: 'bg-surface-muted text-muted border-ink',
    success: 'bg-studojo-green-bg text-emerald-800 border-ink',
    warning: 'bg-studojo-orange-bg text-amber-800 border-ink',
    error: 'bg-red-100 text-red-800 border-ink',
    primary: 'bg-brand-purple-bg text-primary border-ink',
  };

  return (
    <span className={`inline-flex items-center px-3 py-1 rounded-full text-xs font-bold uppercase border-2 ${variants[variant]} ${className}`}>
      {children}
    </span>
  );
}
