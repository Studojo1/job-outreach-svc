import React from 'react';

interface ContainerProps {
  children: React.ReactNode;
  narrow?: boolean;
  className?: string;
}

export function Container({ children, narrow = false, className = '' }: ContainerProps) {
  const maxW = narrow ? 'max-w-onboarding' : 'max-w-[80rem]';
  return (
    <div className={`${maxW} mx-auto px-4 py-8 md:px-8 ${className}`}>
      {children}
    </div>
  );
}
