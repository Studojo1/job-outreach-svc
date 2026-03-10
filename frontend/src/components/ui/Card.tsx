import React from 'react';

interface CardProps {
  children: React.ReactNode;
  hoverable?: boolean;
  className?: string;
  onClick?: () => void;
}

export function Card({ children, hoverable = false, className = '', onClick }: CardProps) {
  const base = 'bg-card border border-border-light rounded-xl shadow-soft p-l';
  const hover = hoverable ? 'transition-all duration-200 hover:border-primary hover:shadow-elevated cursor-pointer' : '';

  return (
    <div className={`${base} ${hover} ${className}`} onClick={onClick}>
      {children}
    </div>
  );
}
