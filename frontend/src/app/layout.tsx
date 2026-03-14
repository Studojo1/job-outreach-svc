import type { Metadata } from 'next';
import '@/styles/globals.css';
import { Providers } from './providers';

export const metadata: Metadata = {
  title: 'OpportunityApply - Job Outreach Tool',
  description: 'Find hiring managers for your dream job',
  icons: {
    icon: '/outreach/favicon.png',
    shortcut: '/outreach/favicon.png',
    apple: '/outreach/favicon.png',
  },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
