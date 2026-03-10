'use client';

import React from 'react';
import Link from 'next/link';
import { useAppStore } from '@/store/useAppStore';
import { LogOut, User } from 'lucide-react';

export function Navbar() {
  const { user, setUser } = useAppStore();

  const handleLogout = () => {
    localStorage.removeItem('token');
    setUser(null);
    window.location.href = '/';
  };

  return (
    <nav className="sticky top-0 z-40 h-16 bg-white border-b border-border-light">
      <div className="max-w-container mx-auto h-full flex items-center justify-between px-l">
        <Link href="/" className="text-h3 text-primary font-bold">
          InternReach
        </Link>

        <div className="flex items-center gap-m">
          {user ? (
            <>
              <div className="flex items-center gap-s text-body-sm text-text-secondary">
                <User className="w-4 h-4" />
                <span>{user.name || user.email}</span>
              </div>
              <button
                onClick={handleLogout}
                className="flex items-center gap-s text-body-sm text-text-secondary hover:text-text-primary transition-colors"
              >
                <LogOut className="w-4 h-4" />
              </button>
            </>
          ) : (
            <Link href="/login" className="btn-primary text-body-sm px-4 py-2 rounded-lg">
              Sign In
            </Link>
          )}
        </div>
      </div>
    </nav>
  );
}
