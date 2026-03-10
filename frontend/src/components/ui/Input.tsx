'use client';

import React from 'react';

interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
}

export function Input({ label, error, className = '', ...props }: InputProps) {
  return (
    <div className="flex flex-col gap-s">
      {label && (
        <label className="text-label uppercase text-text-secondary tracking-wide">
          {label}
        </label>
      )}
      <input
        className={`w-full px-m py-3 border border-border-light rounded-md bg-white text-text-primary
          placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent
          transition-all duration-200 ${error ? 'border-error' : ''} ${className}`}
        {...props}
      />
      {error && <span className="text-body-sm text-error">{error}</span>}
    </div>
  );
}
