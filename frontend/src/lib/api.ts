import axios from 'axios';

export const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000/api/v1';
const PLATFORM_URL = process.env.NEXT_PUBLIC_PLATFORM_URL || '';

const api = axios.create({
  baseURL: API_BASE,
  headers: { 'Content-Type': 'application/json' },
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
 * Calls the platform's /api/auth/token endpoint (same-origin, cookie sent
 * automatically) and stores the JWT in localStorage for control-plane calls.
 * Returns the token string or null if not authenticated.
 */
export async function ensureAuthToken(): Promise<string | null> {
  if (typeof window === 'undefined') return null;

  const existing = localStorage.getItem('token');
  if (existing) return existing;

  try {
    const res = await fetch(`${PLATFORM_URL}/api/auth/token`);
    if (!res.ok) return null;

    const data = await res.json();
    const token = data?.token || data?.accessToken || null;
    if (token) {
      localStorage.setItem('token', token);
    }
    return token;
  } catch {
    return null;
  }
}

export default api;
