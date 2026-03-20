import type { Config } from 'tailwindcss'

const config: Config = {
  content: [
    './src/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      colors: {
        primary: '#8B5CF6',
        'primary-dark': '#7C3AED',
        secondary: '#10B981',
        accent: '#F97316',
        ink: '#171717',
        muted: '#525252',
        'surface-muted': '#F5F5F5',
        'studojo-yellow': '#EAB308',
        'brand-purple-bg': '#FAF5FE',
        'studojo-green-bg': '#D0FAE4',
        'studojo-orange-bg': '#FEF3C6',
        page: '#F9FAFB',
        card: '#FFFFFF',
        'text-primary': '#111827',
        'text-secondary': '#4B5563',
        'border-light': '#E5E7EB',
        success: '#10B981',
        error: '#EF4444',
        warning: '#F59E0B',
      },
      fontFamily: {
        sans: ['Satoshi', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        satoshi: ['Satoshi', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        clash: ['Clash Display', 'ui-sans-serif', 'system-ui', 'sans-serif'],
      },
      fontSize: {
        h1: ['42px', { lineHeight: '1.2', fontWeight: '700' }],
        h2: ['28px', { lineHeight: '1.3', fontWeight: '700' }],
        h3: ['20px', { lineHeight: '1.4', fontWeight: '700' }],
        'body-lg': ['16px', { lineHeight: '1.5' }],
        'body-sm': ['14px', { lineHeight: '1.5' }],
        label: ['12px', { lineHeight: '1.5', fontWeight: '700' }],
      },
      spacing: {
        xs: '4px',
        s: '8px',
        m: '16px',
        l: '24px',
        xl: '32px',
        xxl: '48px',
      },
      borderRadius: {},
      boxShadow: {
        soft: '0 1px 3px 0 rgba(0, 0, 0, 0.1), 0 1px 2px 0 rgba(0, 0, 0, 0.06)',
        elevated: '0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06)',
        brutal: '4px 4px 0px 0px #191a23',
        'brutal-active': '2px 2px 0px 0px #191a23',
        'brutal-xl': '8px 8px 0px 0px #191a23',
      },
      maxWidth: {
        container: '1280px',
        onboarding: '800px',
      },
      animation: {
        'flip': 'flip 600ms cubic-bezier(0.4, 0, 0.2, 1)',
        'slide-in': 'slideIn 300ms ease-out',
        'fade-in': 'fadeIn 300ms ease-out',
        'pulse-soft': 'pulseSoft 2s ease-in-out infinite',
      },
      keyframes: {
        flip: {
          '0%': { transform: 'rotateY(0deg)' },
          '100%': { transform: 'rotateY(180deg)' },
        },
        slideIn: {
          '0%': { opacity: '0', transform: 'translateX(20px)' },
          '100%': { opacity: '1', transform: 'translateX(0)' },
        },
        fadeIn: {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        pulseSoft: {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.5' },
        },
      },
    },
  },
  plugins: [],
}

export default config
