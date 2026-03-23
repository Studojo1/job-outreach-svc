'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { useAppStore } from '@/store/useAppStore';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { ProgressSteps } from '@/components/ui/ProgressSteps';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Spinner } from '@/components/ui/Spinner';
import { User, MapPin, Briefcase, Building2, Target } from 'lucide-react';
import api from '@/lib/api';

export default function ProfilePage() {
  const router = useRouter();
  const { loading: authLoading } = useAuth();
  const { candidateId } = useAppStore();
  const [profile, setProfile] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    if (authLoading || !candidateId) return;

    let retries = 0;
    const maxRetries = 3;

    const fetchProfile = () => {
      api.get(`/candidate/${candidateId}/profile`)
        .then((res) => {
          const parsed = res.data?.parsed_json;
          // If profile_summary exists, the full profile was generated
          if (parsed?.profile_summary || parsed?.career_analysis) {
            console.log('[ProfileGeneration] Profile loaded successfully');
            setProfile(res.data);
            setLoading(false);
          } else if (retries < maxRetries) {
            // Profile may not be generated yet — retry after a delay
            retries++;
            console.log(`[ProfileGeneration] Profile not ready, retrying (${retries}/${maxRetries})...`);
            setTimeout(fetchProfile, 2000);
          } else {
            // Show whatever we have
            setProfile(res.data);
            setLoading(false);
          }
        })
        .catch((err) => {
          setError(err.response?.data?.detail || 'Failed to load profile');
          setLoading(false);
        });
    };

    fetchProfile();
  }, [candidateId]);

  if (!candidateId) {
    return (
      <div className="min-h-screen bg-white">
        <Navbar />
        <Container className="max-w-onboarding py-8 text-center">
          <p className="text-base text-muted mt-8 font-satoshi">Please complete the chat first.</p>
          <Button onClick={() => router.push('/onboarding/chat')} className="mt-6">Go to Chat</Button>
        </Container>
      </div>
    );
  }

  const parsed = profile?.parsed_json || {};
  const personalInfo = parsed.personal_info || {};
  const preferences = parsed.preferences || {};
  const career = parsed.career_analysis || {};

  return (
    <div className="min-h-screen bg-white">
      <Navbar />
      <Container className="max-w-onboarding py-8">
        <ProgressSteps steps={['Upload Resume', 'AI Chat', 'Your Profile']} currentStep={3} />

        <div className="mt-8">
          <h1 className="font-clash text-2xl font-bold mb-2">Your Candidate Profile</h1>
          <p className="text-sm text-muted font-satoshi mb-6">
            Here's what we've learned about you. This powers your lead discovery.
          </p>

          {loading ? (
            <div className="flex justify-center py-12"><Spinner /></div>
          ) : error ? (
            <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-8 text-center">
              <p className="text-error font-satoshi">{error}</p>
              <Button onClick={() => window.location.reload()} className="mt-6">Retry</Button>
            </div>
          ) : (
            <div className="space-y-6 animate-fade-in">
              {/* Summary */}
              {parsed.profile_summary && (
                <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-6">
                  <div className="flex items-center gap-4 mb-4">
                    <User className="w-5 h-5 text-primary" />
                    <h3 className="font-clash text-lg font-bold">Summary</h3>
                  </div>
                  <p className="text-sm text-muted font-satoshi">{parsed.profile_summary}</p>
                </div>
              )}

              {/* Skills */}
              {personalInfo.skills_detected?.length > 0 && (
                <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-6">
                  <div className="flex items-center gap-4 mb-4">
                    <Target className="w-5 h-5 text-primary" />
                    <h3 className="font-clash text-lg font-bold">Skills</h3>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {personalInfo.skills_detected.map((s: string) => (
                      <Badge key={s} variant="primary">{s}</Badge>
                    ))}
                  </div>
                </div>
              )}

              {/* Preferences */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                {preferences.locations?.length > 0 && (
                  <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-6">
                    <div className="flex items-center gap-4 mb-4">
                      <MapPin className="w-5 h-5 text-primary" />
                      <h3 className="font-clash text-lg font-bold">Locations</h3>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      {preferences.locations.map((loc: string) => (
                        <Badge key={loc}>{loc}</Badge>
                      ))}
                    </div>
                  </div>
                )}

                {preferences.industry_interests?.length > 0 && (
                  <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-6">
                    <div className="flex items-center gap-4 mb-4">
                      <Building2 className="w-5 h-5 text-primary" />
                      <h3 className="font-clash text-lg font-bold">Industries</h3>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      {preferences.industry_interests.map((ind: string) => (
                        <Badge key={ind}>{ind}</Badge>
                      ))}
                    </div>
                  </div>
                )}
              </div>

              {/* Recommended Roles */}
              {career.recommended_roles?.length > 0 && (
                <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-6">
                  <div className="flex items-center gap-4 mb-4">
                    <Briefcase className="w-5 h-5 text-primary" />
                    <h3 className="font-clash text-lg font-bold">Recommended Roles</h3>
                  </div>
                  <div className="space-y-4">
                    {career.recommended_roles.map((role: any, i: number) => (
                      <div key={i} className="flex items-center justify-between p-4 bg-surface-muted rounded-xl border-2 border-ink/20">
                        <div>
                          <p className="text-sm font-bold font-satoshi">{role.title}</p>
                          {role.reasoning && (
                            <p className="text-sm text-muted font-satoshi mt-1">{role.reasoning}</p>
                          )}
                        </div>
                        {role.fit_score != null && (
                          <div className="text-right">
                            <span className="font-clash text-lg font-bold text-primary">
                              {Math.round(role.fit_score * 100)}%
                            </span>
                            <span className="text-xs font-bold text-muted uppercase font-satoshi block">fit</span>
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* CTA */}
              <div className="text-center pt-6">
                <Button size="lg" onClick={() => router.push('/leads/discovery')}>
                  Find Decision Makers
                </Button>
              </div>
            </div>
          )}
        </div>
      </Container>
    </div>
  );
}
