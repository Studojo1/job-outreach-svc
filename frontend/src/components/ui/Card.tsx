import React from 'react';

interface CardProps {
  children: React.ReactNode;
  hoverable?: boolean;
  className?: string;
  onClick?: () => void;
}

export function Card({ children, hoverable = false, className = '', onClick }: CardProps) {
  const base = 'rounded-2xl border-2 border-ink bg-white shadow-brutal p-6';
  const hover = hoverable ? 'transition-all hover:translate-x-[2px] hover:translate-y-[2px] hover:shadow-brutal-active cursor-pointer' : '';

  return (
    <div className={`${base} ${hover} ${className}`} onClick={onClick}>
      {children}
    </div>
  );
}
