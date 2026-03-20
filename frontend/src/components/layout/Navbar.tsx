'use client';

import React, { useState, useEffect, useRef } from 'react';
import Link from 'next/link';
import { useAppStore } from '@/store/useAppStore';
import api, { ensureAuthToken } from '@/lib/api';

/**
 * Navbar — mirrors the Studojo platform Header component.
 *
 * Automatically detects auth state via a silent /auth/me check so that
 * users who logged in on the platform see their profile on /outreach too.
 *
 * Logged-in users see a settings-icon dropdown with:
 *   My Resumes, My Applications, My Orders, Settings, Sign out
 *
 * Links to platform-level pages (resumes, applications, orders, settings)
 * use absolute hrefs so the browser navigates to the main Studojo app
 * rather than staying within the /outreach Next.js sub-app.
 */

const STUDOJO_BASE = process.env.NEXT_PUBLIC_PLATFORM_URL || '';

const NAV_LINKS = [
  { href: '/', label: 'Home', internal: false },
  { href: `${STUDOJO_BASE}/blog`, label: 'Blog', internal: false },
  { href: '/', label: 'Outreach', internal: true },
  { href: `${STUDOJO_BASE}/dojos`, label: 'Dojos', internal: false },
  { href: `${STUDOJO_BASE}/reviews`, label: 'Reviews', internal: false },
] as const;

const USER_MENU_LINKS = [
  { href: `${STUDOJO_BASE}/resumes`, label: 'My Resumes', icon: 'resume', internal: false },
  { href: `${STUDOJO_BASE}/my-applications`, label: 'My Applications', icon: 'briefcase', internal: false },
  { href: '/orders', label: 'My Orders', icon: 'order', internal: true },
  { href: `${STUDOJO_BASE}/settings`, label: 'Settings', icon: 'settings', internal: false },
] as const;

function MenuIcon({ name }: { name: string }) {
  switch (name) {
    case 'resume':
      return (
        <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
        </svg>
      );
    case 'briefcase':
      return (
        <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 13.255A23.931 23.931 0 0112 15c-3.183 0-6.22-.62-9-1.745M16 6V4a2 2 0 00-2-2h-4a2 2 0 00-2 2v2m4 6h.01M5 20h14a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
        </svg>
      );
    case 'order':
      return (
        <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" />
        </svg>
      );
    case 'settings':
      return (
        <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
        </svg>
      );
    default:
      return null;
  }
}

export function Navbar() {
  const { user, setUser } = useAppStore();
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const userMenuRef = useRef<HTMLDivElement>(null);

  // Silent auth check — exchange BetterAuth cookie for JWT, then fetch user
  useEffect(() => {
    if (user) return;
    ensureAuthToken()
      .then(() => api.get('/auth/me'))
      .then((res) => {
        const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
        setUser({ ...res.data, timezone: res.data.timezone || browserTz });
      })
      .catch(() => { /* not logged in — ignore */ });
  }, []);

  const handleSignOut = () => {
    setUser(null);
    window.location.href = '/';
  };

  // Close dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (userMenuRef.current && !userMenuRef.current.contains(e.target as Node)) {
        setUserMenuOpen(false);
      }
    };
    if (userMenuOpen) document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [userMenuOpen]);

  return (
    <header className="sticky top-0 z-50 w-full border-b border-ink bg-white">
      <div className="mx-auto flex h-16 max-w-[80rem] items-center justify-between px-4 md:h-24 md:px-8">
        {/* Brand */}
        <a
          href="/"
          className="font-satoshi text-2xl font-black leading-9 text-ink md:text-4xl md:leading-7"
        >
          studojo
        </a>

        {/* Desktop nav */}
        <nav className="hidden items-center gap-8 md:flex" aria-label="Main">
          {NAV_LINKS.map((link) =>
            link.internal ? (
              <Link
                key={link.label}
                href={link.href}
                className="font-satoshi text-base font-bold leading-6 text-secondary"
              >
                {link.label}
              </Link>
            ) : (
              <a
                key={link.label}
                href={link.href}
                className="font-satoshi text-base leading-6 text-muted hover:text-ink transition-colors"
              >
                {link.label}
              </a>
            )
          )}
        </nav>

        <div className="flex items-center gap-4">
          {user ? (
            <>
              {/* User dropdown — mirrors Studojo Header */}
              <div className="relative hidden sm:block" ref={userMenuRef}>
                <button
                  type="button"
                  onClick={() => setUserMenuOpen(!userMenuOpen)}
                  className="flex items-center gap-2 font-satoshi text-base font-medium leading-6 text-muted hover:text-ink"
                >
                  <MenuIcon name="settings" />
                  <span>{user.name || user.email}</span>
                  <svg
                    className={`h-4 w-4 transition-transform ${userMenuOpen ? 'rotate-180' : ''}`}
                    fill="none"
                    stroke="currentColor"
                    viewBox="0 0 24 24"
                  >
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                  </svg>
                </button>
                {userMenuOpen && (
                  <div className="absolute right-0 top-full mt-2 w-56 rounded-lg border-2 border-ink bg-white shadow-lg z-50">
                    <div className="py-2">
                      {USER_MENU_LINKS.map((item) =>
                        item.internal ? (
                          <Link
                            key={item.label}
                            href={item.href}
                            onClick={() => setUserMenuOpen(false)}
                            className="flex items-center gap-3 px-4 py-2 font-satoshi text-sm text-muted hover:bg-surface-muted"
                          >
                            <MenuIcon name={item.icon} />
                            <span>{item.label}</span>
                          </Link>
                        ) : (
                          <a
                            key={item.label}
                            href={item.href}
                            onClick={() => setUserMenuOpen(false)}
                            className="flex items-center gap-3 px-4 py-2 font-satoshi text-sm text-muted hover:bg-surface-muted"
                          >
                            <MenuIcon name={item.icon} />
                            <span>{item.label}</span>
                          </a>
                        )
                      )}
                      <div className="my-1 border-t border-ink/10" />
                      <button
                        type="button"
                        onClick={() => { setUserMenuOpen(false); handleSignOut(); }}
                        className="flex w-full items-center gap-3 px-4 py-2 font-satoshi text-sm text-muted hover:bg-surface-muted"
                      >
                        <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
                        </svg>
                        <span>Sign out</span>
                      </button>
                    </div>
                  </div>
                )}
              </div>

              {/* Mobile: settings icon */}
              <a
                href={`${STUDOJO_BASE}/settings`}
                className="flex h-10 w-10 items-center justify-center rounded-lg text-muted hover:bg-surface-muted sm:hidden"
                aria-label="Settings"
              >
                <MenuIcon name="settings" />
              </a>
            </>
          ) : (
            <>
              <a
                href={`${STUDOJO_BASE}/auth?mode=signin&redirect=/outreach`}
                className="hidden font-satoshi text-base font-medium leading-6 text-muted sm:block"
              >
                Sign In
              </a>
              <a
                href={`${STUDOJO_BASE}/auth?mode=signup`}
                className="flex h-12 items-center justify-center rounded-2xl bg-ink px-4 font-satoshi text-sm font-medium leading-6 text-white transition-transform hover:translate-x-[2px] hover:translate-y-[2px] max-w-[120px] flex-shrink-0 md:w-32 md:text-base md:max-w-none"
              >
                Get Started
              </a>
            </>
          )}

          {/* Mobile hamburger */}
          <button
            type="button"
            onClick={() => setMobileOpen((o) => !o)}
            className="flex h-10 w-10 items-center justify-center rounded-lg text-ink hover:bg-surface-muted md:hidden"
            aria-expanded={mobileOpen}
            aria-label="Toggle menu"
          >
            {mobileOpen ? (
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M18 6L6 18M6 6l12 12" />
              </svg>
            ) : (
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M4 6h16M4 12h16M4 18h16" />
              </svg>
            )}
          </button>
        </div>
      </div>

      {/* Mobile menu */}
      {mobileOpen && (
        <nav className="border-t border-ink/10 bg-white px-8 py-4 md:hidden" aria-label="Mobile menu">
          <ul className="flex flex-col gap-2">
            {NAV_LINKS.map(({ href, label, internal }) => (
              <li key={label}>
                {internal ? (
                  <Link
                    href={href}
                    onClick={() => setMobileOpen(false)}
                    className="block rounded-lg py-2 font-satoshi font-bold text-secondary"
                  >
                    {label}
                  </Link>
                ) : (
                  <a
                    href={href}
                    onClick={() => setMobileOpen(false)}
                    className="block rounded-lg py-2 font-satoshi text-muted hover:bg-surface-muted"
                  >
                    {label}
                  </a>
                )}
              </li>
            ))}
            {user ? (
              <>
                {USER_MENU_LINKS.map((item) => (
                  <li key={item.label}>
                    {item.internal ? (
                      <Link
                        href={item.href}
                        onClick={() => setMobileOpen(false)}
                        className="block rounded-lg py-2 font-satoshi text-muted hover:bg-surface-muted"
                      >
                        {item.label}
                      </Link>
                    ) : (
                      <a
                        href={item.href}
                        onClick={() => setMobileOpen(false)}
                        className="block rounded-lg py-2 font-satoshi text-muted hover:bg-surface-muted"
                      >
                        {item.label}
                      </a>
                    )}
                  </li>
                ))}
                <li>
                  <button
                    type="button"
                    onClick={() => { setMobileOpen(false); handleSignOut(); }}
                    className="block w-full rounded-lg py-2 text-left font-satoshi text-muted hover:bg-surface-muted"
                  >
                    Sign out
                  </button>
                </li>
              </>
            ) : (
              <>
                <li>
                  <a
                    href={`${STUDOJO_BASE}/auth?mode=signin&redirect=/outreach`}
                    onClick={() => setMobileOpen(false)}
                    className="block rounded-lg py-2 font-satoshi text-muted hover:bg-surface-muted"
                  >
                    Sign In
                  </a>
                </li>
                <li>
                  <a
                    href={`${STUDOJO_BASE}/auth?mode=signup`}
                    onClick={() => setMobileOpen(false)}
                    className="block rounded-lg py-2 font-satoshi text-muted hover:bg-surface-muted"
                  >
                    Get Started
                  </a>
                </li>
              </>
            )}
          </ul>
        </nav>
      )}
    </header>
  );
}
