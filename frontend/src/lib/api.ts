import axios from 'axios';

export const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000/api/v1';
const PLATFORM_URL = process.env.NEXT_PUBLIC_PLATFORM_URL || '';

const api = axios.create({
  baseURL: API_BASE,
  headers: { 'Content-Type': 'application/json' },
  withCredentials: true, // send BetterAuth session cookie on every request (fallback auth)
});

// Attach JWT on every request
api.interceptors.request.use((config) => {
  if (typeof window !== 'undefined') {
    const token = localStorage.getItem('token');
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
  }
  return config;
});

// Handle 401 — reject so callers (useAuth, Navbar) can handle redirect
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401 && typeof window !== 'undefined') {
      localStorage.removeItem('token');
    }
    return Promise.reject(error);
  }
);

/**
 * Exchange BetterAuth session cookie for a JWT token.
 * Tries the platform /api/auth/token endpoint first; falls back to the outreach
 * backend /auth/token endpoint. If neither works, returns null — the backend
 * also accepts the BetterAuth session cookie directly on same-origin requests,
 * so axios calls still authenticate via the cookie even without a JWT.
 */
export async function ensureAuthToken(): Promise<string | null> {
  if (typeof window === 'undefined') return null;

  const existing = localStorage.getItem('token');
  if (existing) return existing;

  // Try platform token endpoint first, then outreach backend
  const endpoints = [
    `${PLATFORM_URL}/api/auth/token`,
    `${API_BASE}/auth/token`,
  ];

  for (const endpoint of endpoints) {
    try {
      const res = await fetch(endpoint, { credentials: 'include' });
      if (!res.ok) continue;
      const data = await res.json();
      const token = data?.token || data?.accessToken || null;
      if (token) {
        localStorage.setItem('token', token);
        return token;
      }
    } catch {
      // try next endpoint
    }
  }

  // Both failed — backend accepts session cookie on same-origin requests, so
  // we proceed without a JWT. The cookie is sent automatically by the browser.
  return null;
}

export default api;
