'use client';

import { useRouter } from 'next/navigation';
import { Button } from '@/components/ui/Button';
import { Container } from '@/components/layout/Container';
import { Upload, Search, Mail, ArrowRight } from 'lucide-react';

export default function LandingPage() {
  const router = useRouter();

  const features = [
    {
      icon: <Upload className="w-6 h-6" />,
      title: 'Upload Your Resume',
      desc: 'Our AI analyzes your background and career goals in under a minute.',
    },
    {
      icon: <Search className="w-6 h-6" />,
      title: 'Discover Decision Makers',
      desc: 'We find hiring managers at companies that match your profile.',
    },
    {
      icon: <Mail className="w-6 h-6" />,
      title: 'Launch Outreach',
      desc: 'Send personalized emails at scale with intelligent scheduling.',
    },
  ];

  return (
    <div className="min-h-screen">
      {/* Hero */}
      <section className="bg-gradient-to-br from-primary via-primary-dark to-purple-900 text-white py-24">
        <Container className="text-center">
          <h1 className="text-h1 max-w-3xl mx-auto leading-tight">
            Find Hiring Managers for Your Dream Job
          </h1>
          <p className="text-body-lg text-white/80 mt-l max-w-xl mx-auto">
            InternReach uses AI to discover decision makers, enrich contacts, and launch personalized outreach campaigns — all from your resume.
          </p>
          <div className="mt-xl">
            <Button
              variant="ghost"
              size="lg"
              onClick={() => router.push('/login')}
              className="!bg-white !text-primary hover:!bg-gray-50 hover:!text-primary"
            >
              Get Started <ArrowRight className="w-5 h-5 ml-s inline" />
            </Button>
          </div>
        </Container>
      </section>

      {/* Features */}
      <section className="py-24">
        <Container>
          <h2 className="text-h2 text-center mb-xl">How It Works</h2>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-xl">
            {features.map((f, i) => (
              <div
                key={i}
                className="card-hover text-center p-xl"
              >
                <div className="w-14 h-14 rounded-xl bg-primary/10 flex items-center justify-center mx-auto text-primary mb-l">
                  {f.icon}
                </div>
                <h3 className="text-h3 mb-s">{f.title}</h3>
                <p className="text-body-sm text-text-secondary">{f.desc}</p>
              </div>
            ))}
          </div>
        </Container>
      </section>

      {/* CTA */}
      <section className="bg-gray-50 py-24">
        <Container className="text-center">
          <h2 className="text-h2 mb-m">Ready to find your next opportunity?</h2>
          <p className="text-body-lg text-text-secondary mb-xl">
            Upload your resume and let AI do the outreach.
          </p>
          <Button onClick={() => router.push('/login')}>
            Start Now <ArrowRight className="w-4 h-4 ml-s inline" />
          </Button>
        </Container>
      </section>
    </div>
  );
}