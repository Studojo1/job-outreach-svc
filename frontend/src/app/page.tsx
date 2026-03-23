'use client';

import { useRouter } from 'next/navigation';
import Link from 'next/link';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { Navbar } from '@/components/layout/Navbar';
import { Upload, Search, Mail, ArrowRight, ClipboardList } from 'lucide-react';

const STUDOJO_BASE = process.env.NEXT_PUBLIC_PLATFORM_URL || '';

const DOJO_LINKS = [
  { href: `${STUDOJO_BASE}/dojos/assignment`, label: 'Assignment Dojo', desc: 'Master your assignments', color: 'bg-violet-500', internal: false },
  { href: '/onboarding/upload', label: 'Opportunity Apply', desc: 'Reach hiring managers directly', color: 'bg-emerald-500', internal: true },
];

const SOCIAL_LINKS = [
  { href: 'https://www.linkedin.com/company/studojo/', label: 'LinkedIn' },
  { href: 'https://instagram.com/studojo', label: 'Instagram' },
  { href: 'https://chat.whatsapp.com/CUV8DSjQWqB82yXKRE66ol?mode=gi_t', label: 'WhatsApp' },
];

function SocialIcon({ label }: { label: string }) {
  switch (label) {
    case 'LinkedIn':
      return (
        <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 24 24">
          <path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 01-2.063-2.065 2.064 2.064 0 112.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/>
        </svg>
      );
    case 'Instagram':
      return (
        <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 24 24">
          <path d="M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zM12 0C8.741 0 8.333.014 7.053.072 2.695.272.273 2.69.073 7.052.014 8.333 0 8.741 0 12c0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98C8.333 23.986 8.741 24 12 24c3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98C15.668.014 15.259 0 12 0zm0 5.838a6.162 6.162 0 100 12.324 6.162 6.162 0 000-12.324zM12 16a4 4 0 110-8 4 4 0 010 8zm6.406-11.845a1.44 1.44 0 100 2.881 1.44 1.44 0 000-2.881z"/>
        </svg>
      );
    case 'WhatsApp':
      return (
        <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 24 24">
          <path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413z"/>
        </svg>
      );
    default:
      return <span className="text-sm font-bold">{label[0]}</span>;
  }
}

export default function LandingPage() {
  const router = useRouter();

  const features = [
    { icon: <Upload className="w-6 h-6" />, title: 'Upload Your Resume', desc: 'Our AI analyzes your background and career goals in under a minute.' },
    { icon: <Search className="w-6 h-6" />, title: 'Discover Decision Makers', desc: 'We find hiring managers at companies that match your profile.' },
    { icon: <Mail className="w-6 h-6" />, title: 'Launch Outreach', desc: 'Send personalized emails at scale with intelligent scheduling.' },
  ];

  return (
    <div className="w-full bg-white">
      <Navbar />

      {/* Hero */}
      <section className="border-b border-ink bg-gradient-to-br from-violet-700 via-purple-700 to-violet-800">
        <div className="mx-auto max-w-[80rem] px-4 pt-8 pb-8 md:px-8 md:py-20">
          <div className="flex flex-col gap-6 text-center md:gap-8 md:text-left">
            <h1 className="max-w-3xl font-clash text-3xl font-semibold leading-tight tracking-tight text-white md:text-4xl lg:text-5xl">
              Find Hiring Managers for Your Dream Job
            </h1>
            <p className="max-w-xl font-satoshi text-sm font-normal leading-6 text-white/90 md:text-base md:leading-7">
              OpportunityApply uses AI to discover decision makers, enrich contacts, and launch personalized outreach campaigns — all from your resume.
            </p>
            <div className="flex flex-col gap-4 md:flex-row md:flex-wrap">
              <Button
                onClick={() => router.push('/onboarding/upload')}
                size="lg"
                variant="accent"
              >
                Get Started <ArrowRight className="w-5 h-5 ml-2 inline" />
              </Button>
              <Button
                onClick={() => router.push('/orders')}
                size="lg"
                variant="ghost"
                className="border-2 border-white/40 text-white hover:bg-white/10"
              >
                <ClipboardList className="w-5 h-5 mr-2 inline" /> View Campaigns
              </Button>
            </div>
          </div>
        </div>
      </section>

      {/* Features */}
      <section className="border-b border-ink bg-white">
        <div className="mx-auto max-w-[80rem] px-4 pt-8 pb-8 md:px-8 md:pt-24 md:pb-16">
          <h2 className="font-clash text-3xl font-bold text-center mb-12">How It Works</h2>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
            {features.map((f, i) => (
              <Card key={i} hoverable className="text-center">
                <div className="w-14 h-14 rounded-xl bg-purple-100 border-2 border-ink flex items-center justify-center mx-auto text-primary mb-6">
                  {f.icon}
                </div>
                <h3 className="font-clash text-xl font-bold mb-2">{f.title}</h3>
                <p className="text-sm text-muted font-satoshi">{f.desc}</p>
              </Card>
            ))}
          </div>
        </div>
      </section>

      {/* CTA */}
      <section className="border-b border-ink bg-gradient-to-br from-purple-100 via-violet-100 to-purple-50">
        <div className="mx-auto max-w-[80rem] px-4 pt-8 pb-8 md:px-8 md:py-24 text-center">
          <h2 className="font-clash text-3xl font-bold mb-4">Ready to find your next opportunity?</h2>
          <p className="text-lg text-muted mb-10 font-satoshi">
            Upload your resume and let AI do the outreach.
          </p>
          <Button onClick={() => router.push('/onboarding/upload')} size="lg">
            Start Now <ArrowRight className="w-4 h-4 ml-2 inline" />
          </Button>
        </div>
      </section>

      {/* Footer */}
      <footer className="relative border-b border-ink bg-white">
        <div className="mx-auto max-w-[80rem] px-4 pt-8 py-8 md:px-8 md:py-24">
          <div className="flex flex-col gap-8 md:grid md:grid-cols-2 md:gap-16">
            {/* Left: branding + Join the Dojo + contact */}
            <div className="flex flex-col gap-8 md:gap-12">
              <a href="/" className="font-satoshi text-2xl font-black tracking-tight text-ink md:text-3xl">
                studojo
              </a>

              <div className="flex flex-col gap-3 rounded-2xl border border-ink/20 bg-white p-6 md:rounded-3xl md:bg-purple-50 md:gap-4 md:p-8">
                <h3 className="font-clash text-lg font-medium text-ink md:text-2xl">Join the Dojo</h3>
                <p className="font-satoshi text-sm text-muted md:text-base">
                  Get weekly wisdom, tips, and exclusive student insights
                </p>
              </div>

              <div className="flex items-center gap-3">
                <span className="flex h-10 w-10 items-center justify-center rounded-full bg-purple-100 text-primary">
                  <Mail className="w-4 h-4" />
                </span>
                <span className="font-satoshi text-sm text-ink md:text-base">admin@studojo.com</span>
              </div>
            </div>

            {/* Right: Explore Our Dojos + links */}
            <div className="flex flex-col gap-8">
              <div className="hidden md:block">
                <h3 className="font-satoshi text-2xl font-black tracking-tight text-ink">Explore Our Dojos</h3>
                <ul className="mt-6 flex flex-col gap-6">
                  {DOJO_LINKS.map(({ href, label, desc, color, internal }) => (
                    <li key={label}>
                      {internal ? (
                        <Link href={href} className={`flex items-center justify-between rounded-2xl ${color} p-6 transition opacity-90 hover:opacity-100`}>
                          <div>
                            <p className="font-clash text-2xl font-medium text-white">{label}</p>
                            <p className="font-satoshi text-sm text-white/80">{desc}</p>
                          </div>
                          <span className="text-white" aria-hidden>&#8594;</span>
                        </Link>
                      ) : (
                        <a href={href} className={`flex items-center justify-between rounded-2xl ${color} p-6 transition opacity-90 hover:opacity-100`}>
                          <div>
                            <p className="font-clash text-2xl font-medium text-white">{label}</p>
                            <p className="font-satoshi text-sm text-white/80">{desc}</p>
                          </div>
                          <span className="text-white" aria-hidden>&#8594;</span>
                        </a>
                      )}
                    </li>
                  ))}
                </ul>
              </div>

              <div className="grid grid-cols-2 gap-8">
                <div>
                  <h3 className="font-satoshi text-xs font-medium text-ink md:text-base md:font-black">Help Center</h3>
                  <ul className="mt-3 flex flex-col gap-2 md:mt-4 md:gap-3">
                    <li>
                      <a href="mailto:admin@studojo.com" className="font-satoshi text-xs text-muted md:text-base hover:underline">Contact Support</a>
                    </li>
                    <li>
                      <a href="https://chat.whatsapp.com/CUV8DSjQWqB82yXKRE66ol?mode=gi_t" className="font-satoshi text-xs text-muted md:text-base hover:underline">Community</a>
                    </li>
                  </ul>
                </div>
              </div>
            </div>
          </div>

          {/* Social links — proper SVG icons */}
          <div className="mt-8 flex flex-col gap-4 border-t border-ink/10 pt-6 md:mt-16 md:border-y md:border-ink/20 md:py-12">
            <div className="flex flex-col items-center gap-4 text-center">
              <p className="font-satoshi text-xs text-muted md:text-base">
                Connect with thousands of students reaching their journey
              </p>
              <div className="flex gap-3 md:gap-4">
                {SOCIAL_LINKS.map(({ href, label }) => (
                  <a
                    key={label}
                    href={href}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="flex h-10 w-10 items-center justify-center rounded-full bg-purple-100 text-primary transition hover:bg-purple-200 md:h-14 md:w-14 md:rounded-2xl"
                    aria-label={label}
                  >
                    <SocialIcon label={label} />
                  </a>
                ))}
              </div>
            </div>
          </div>

          {/* Legal */}
          <div className="mt-8 flex flex-col items-center justify-between gap-4 border-t border-ink/10 pt-6 md:mt-8 md:flex-row md:border-0 md:pt-0">
            <p className="text-center font-satoshi text-xs text-muted md:text-lg">
              &copy; 2025 Studojo. Crafted with ❤️ by students
            </p>
            <div className="flex flex-wrap justify-center gap-4 md:gap-8">
              <a href={`${STUDOJO_BASE}/privacy`} className="font-satoshi text-xs text-muted md:text-lg hover:underline">Privacy Policy</a>
              <a href={`${STUDOJO_BASE}/terms`} className="font-satoshi text-xs text-muted md:text-lg hover:underline">Terms of Service</a>
              <a href={`${STUDOJO_BASE}/refund-policy`} className="font-satoshi text-xs text-muted md:text-lg hover:underline">Refund Policy</a>
            </div>
          </div>
        </div>

        {/* Giant studojo text — matches platform footer */}
        <div className="relative flex w-full items-center justify-center overflow-hidden px-2 pb-4 pt-8 md:px-0 md:pb-8 md:pt-16">
          <span
            className="pointer-events-none select-none whitespace-nowrap font-clash font-semibold leading-[0.6] tracking-tight text-purple-100/60 text-[clamp(72px,22vw,180px)] md:text-[min(356px,40vw)]"
            aria-hidden
          >
            studojo
          </span>
        </div>
      </footer>
    </div>
  );
}
