import React from 'react';

interface BadgeProps {
  children: React.ReactNode;
  variant?: 'default' | 'success' | 'warning' | 'error' | 'primary';
  className?: string;
}

export function Badge({ children, variant = 'default', className = '' }: BadgeProps) {
  const variants = {
    default: 'bg-gray-100 text-text-secondary',
    success: 'bg-emerald-100 text-emerald-700',
    warning: 'bg-amber-100 text-amber-700',
    error: 'bg-red-100 text-red-700',
    primary: 'bg-purple-100 text-primary',
  };

  return (
    <span className={`inline-flex items-center px-3 py-1 rounded-full text-label uppercase ${variants[variant]} ${className}`}>
      {children}
    </span>
  );
}
