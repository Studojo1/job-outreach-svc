import React from 'react';

interface ContainerProps {
  children: React.ReactNode;
  narrow?: boolean;
  className?: string;
}

export function Container({ children, narrow = false, className = '' }: ContainerProps) {
  const maxW = narrow ? 'max-w-onboarding' : 'max-w-container';
  return (
    <div className={`${maxW} mx-auto px-l py-xl ${className}`}>
      {children}
    </div>
  );
}
