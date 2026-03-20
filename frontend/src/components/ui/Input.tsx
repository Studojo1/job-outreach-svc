'use client';

import React from 'react';

interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
}

export function Input({ label, error, className = '', ...props }: InputProps) {
  return (
    <div className="flex flex-col gap-2">
      {label && (
        <label className="text-xs font-bold uppercase text-muted tracking-wide font-satoshi">
          {label}
        </label>
      )}
      <input
        className={`w-full px-4 py-3 border-2 border-ink/20 rounded-xl bg-white text-ink font-satoshi
          placeholder:text-neutral-400 focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent
          transition-all duration-200 ${error ? 'border-error' : ''} ${className}`}
        {...props}
      />
      {error && <span className="text-sm text-error font-satoshi">{error}</span>}
    </div>
  );
}
