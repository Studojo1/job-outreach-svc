'use client';

import { useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { useAppStore } from '@/store/useAppStore';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { ProgressSteps } from '@/components/ui/ProgressSteps';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Upload, FileText, CheckCircle } from 'lucide-react';
import api from '@/lib/api';
import type { ResumePreview } from '@/lib/types/candidate';

export default function UploadPage() {
  const router = useRouter();
  useAuth();
  const { setCandidateId, setCurrentStep } = useAppStore();
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [preview, setPreview] = useState<ResumePreview | null>(null);
  const [candidateIdLocal, setCandidateIdLocal] = useState<number | null>(null);
  const [error, setError] = useState('');

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    const f = e.dataTransfer.files[0];
    if (f && (f.type === 'application/pdf' || f.name.endsWith('.docx'))) {
      setFile(f);
      setError('');
    } else {
      setError('Please upload a PDF or DOCX file');
    }
  }, []);

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (f) {
      setFile(f);
      setError('');
    }
  };

  const handleUpload = async () => {
    if (!file) return;
    setUploading(true);
    setError('');
    try {
      const formData = new FormData();
      formData.append('file', file);
      const res = await api.post('/candidate/upload', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
      });
      setPreview(res.data.preview);
      setCandidateIdLocal(res.data.candidate_id);
      setCandidateId(res.data.candidate_id);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Upload failed. Please try again.');
    } finally {
      setUploading(false);
    }
  };

  const handleContinue = () => {
    setCurrentStep(2);
    router.push('/onboarding/chat');
  };

  return (
    <div className="min-h-screen bg-surface-muted">
      <Navbar />
      <Container className="max-w-onboarding py-8">
        <ProgressSteps steps={['Upload Resume', 'AI Chat', 'Your Profile']} currentStep={1} />

        <div className="mt-8">
          {!preview ? (
            <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-8">
              <h1 className="font-clash text-2xl font-bold mb-2">Upload Your Resume</h1>
              <p className="text-sm text-muted font-satoshi mb-8">
                We'll analyze your background to find the right decision makers.
              </p>

              <div
                onDragOver={(e) => e.preventDefault()}
                onDrop={handleDrop}
                className="border-2 border-dashed border-ink/30 rounded-2xl p-12 text-center hover:border-primary hover:bg-brand-purple-bg/50 transition-all cursor-pointer"
                onClick={() => document.getElementById('file-input')?.click()}
              >
                <Upload className="w-12 h-12 text-muted mx-auto mb-4" />
                <p className="text-base text-ink font-satoshi mb-2">
                  {file ? file.name : 'Drop your resume here, or click to browse'}
                </p>
                <p className="text-sm text-muted font-satoshi">PDF or DOCX, up to 10MB</p>
                <input
                  id="file-input"
                  type="file"
                  accept=".pdf,.docx"
                  onChange={handleFileSelect}
                  className="hidden"
                />
              </div>

              {file && (
                <div className="flex items-center gap-4 mt-6 p-4 bg-surface-muted rounded-xl border-2 border-ink/20">
                  <FileText className="w-5 h-5 text-primary" />
                  <span className="text-sm flex-1 font-satoshi">{file.name}</span>
                  <span className="text-xs font-bold text-muted uppercase font-satoshi">
                    {(file.size / 1024).toFixed(0)} KB
                  </span>
                </div>
              )}

              {error && (
                <p className="text-sm text-error mt-4 font-satoshi">{error}</p>
              )}

              <div className="mt-6">
                <Button onClick={handleUpload} loading={uploading} disabled={!file}>
                  Upload & Analyze
                </Button>
              </div>
            </div>
          ) : (
            <div className="rounded-2xl border-2 border-ink bg-white shadow-brutal p-8 animate-fade-in">
              <div className="flex items-center gap-4 mb-6">
                <CheckCircle className="w-6 h-6 text-secondary" />
                <h2 className="font-clash text-2xl font-bold">Resume Analyzed</h2>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                {preview.name && (
                  <div>
                    <span className="text-xs font-bold text-muted uppercase font-satoshi">Name</span>
                    <p className="text-base mt-1 font-satoshi">{preview.name}</p>
                  </div>
                )}
                {preview.email && (
                  <div>
                    <span className="text-xs font-bold text-muted uppercase font-satoshi">Email</span>
                    <p className="text-base mt-1 font-satoshi">{preview.email}</p>
                  </div>
                )}
                {preview.experience_years != null && (
                  <div>
                    <span className="text-xs font-bold text-muted uppercase font-satoshi">Experience</span>
                    <p className="text-base mt-1 font-satoshi">{preview.experience_years} years</p>
                  </div>
                )}
                {preview.char_count != null && (
                  <div>
                    <span className="text-xs font-bold text-muted uppercase font-satoshi">Resume Length</span>
                    <p className="text-base mt-1 font-satoshi">{preview.char_count.toLocaleString()} characters</p>
                  </div>
                )}
              </div>

              {preview.skills && preview.skills.length > 0 && (
                <div className="mt-6">
                  <span className="text-xs font-bold text-muted uppercase font-satoshi">Skills Detected</span>
                  <div className="flex flex-wrap gap-2 mt-2">
                    {preview.skills.map((s) => (
                      <Badge key={s} variant="primary">{s}</Badge>
                    ))}
                  </div>
                </div>
              )}

              {preview.education && preview.education.length > 0 && (
                <div className="mt-6">
                  <span className="text-xs font-bold text-muted uppercase font-satoshi">Education</span>
                  <div className="flex flex-wrap gap-2 mt-2">
                    {preview.education.map((e) => (
                      <Badge key={e}>{e}</Badge>
                    ))}
                  </div>
                </div>
              )}

              <div className="mt-8">
                <Button onClick={handleContinue}>
                  Continue to AI Chat
                </Button>
              </div>
            </div>
          )}
        </div>
      </Container>
    </div>
  );
}