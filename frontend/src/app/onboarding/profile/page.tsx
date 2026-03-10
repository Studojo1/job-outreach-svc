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
  useAuth();
  const { candidateId } = useAppStore();
  const [profile, setProfile] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!candidateId) return;

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
      <div className="min-h-screen bg-page">
        <Navbar />
        <Container className="max-w-onboarding py-xl text-center">
          <p className="text-body-lg text-text-secondary mt-xl">Please complete the chat first.</p>
          <Button onClick={() => router.push('/onboarding/chat')} className="mt-l">Go to Chat</Button>
        </Container>
      </div>
    );
  }

  const parsed = profile?.parsed_json || {};
  const personalInfo = parsed.personal_info || {};
  const preferences = parsed.preferences || {};
  const career = parsed.career_analysis || {};

  return (
    <div className="min-h-screen bg-page">
      <Navbar />
      <Container className="max-w-onboarding py-xl">
        <ProgressSteps steps={['Upload Resume', 'AI Chat', 'Your Profile']} currentStep={3} />

        <div className="mt-xl">
          <h1 className="text-h2 mb-s">Your Candidate Profile</h1>
          <p className="text-body-sm text-text-secondary mb-l">
            Here's what we've learned about you. This powers your lead discovery.
          </p>

          {loading ? (
            <div className="flex justify-center py-xxl"><Spinner /></div>
          ) : error ? (
            <div className="card p-xl text-center">
              <p className="text-error">{error}</p>
              <Button onClick={() => window.location.reload()} className="mt-l">Retry</Button>
            </div>
          ) : (
            <div className="space-y-l animate-fade-in">
              {/* Summary */}
              {parsed.profile_summary && (
                <div className="card p-l">
                  <div className="flex items-center gap-m mb-m">
                    <User className="w-5 h-5 text-primary" />
                    <h3 className="text-h3">Summary</h3>
                  </div>
                  <p className="text-body-sm text-text-secondary">{parsed.profile_summary}</p>
                </div>
              )}

              {/* Skills */}
              {personalInfo.skills_detected?.length > 0 && (
                <div className="card p-l">
                  <div className="flex items-center gap-m mb-m">
                    <Target className="w-5 h-5 text-primary" />
                    <h3 className="text-h3">Skills</h3>
                  </div>
                  <div className="flex flex-wrap gap-s">
                    {personalInfo.skills_detected.map((s: string) => (
                      <Badge key={s} variant="primary">{s}</Badge>
                    ))}
                  </div>
                </div>
              )}

              {/* Preferences */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-l">
                {preferences.locations?.length > 0 && (
                  <div className="card p-l">
                    <div className="flex items-center gap-m mb-m">
                      <MapPin className="w-5 h-5 text-primary" />
                      <h3 className="text-h3">Locations</h3>
                    </div>
                    <div className="flex flex-wrap gap-s">
                      {preferences.locations.map((loc: string) => (
                        <Badge key={loc}>{loc}</Badge>
                      ))}
                    </div>
                  </div>
                )}

                {preferences.industry_interests?.length > 0 && (
                  <div className="card p-l">
                    <div className="flex items-center gap-m mb-m">
                      <Building2 className="w-5 h-5 text-primary" />
                      <h3 className="text-h3">Industries</h3>
                    </div>
                    <div className="flex flex-wrap gap-s">
                      {preferences.industry_interests.map((ind: string) => (
                        <Badge key={ind}>{ind}</Badge>
                      ))}
                    </div>
                  </div>
                )}
              </div>

              {/* Recommended Roles */}
              {career.recommended_roles?.length > 0 && (
                <div className="card p-l">
                  <div className="flex items-center gap-m mb-m">
                    <Briefcase className="w-5 h-5 text-primary" />
                    <h3 className="text-h3">Recommended Roles</h3>
                  </div>
                  <div className="space-y-m">
                    {career.recommended_roles.map((role: any, i: number) => (
                      <div key={i} className="flex items-center justify-between p-m bg-gray-50 rounded-lg">
                        <div>
                          <p className="text-body-sm font-semibold">{role.title}</p>
                          {role.reasoning && (
                            <p className="text-body-sm text-text-secondary mt-xs">{role.reasoning}</p>
                          )}
                        </div>
                        {role.fit_score != null && (
                          <div className="text-right">
                            <span className="text-h3 text-primary">
                              {Math.round(role.fit_score * 100)}%
                            </span>
                            <span className="text-label text-text-secondary block">fit</span>
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* CTA */}
              <div className="text-center pt-l">
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