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
    <div className="min-h-screen bg-page">
      <Navbar />
      <Container className="max-w-onboarding py-xl">
        <ProgressSteps steps={['Upload Resume', 'AI Chat', 'Your Profile']} currentStep={1} />

        <div className="mt-xl">
          {!preview ? (
            <div className="card p-xl">
              <h1 className="text-h2 mb-s">Upload Your Resume</h1>
              <p className="text-body-sm text-text-secondary mb-xl">
                We'll analyze your background to find the right decision makers.
              </p>

              <div
                onDragOver={(e) => e.preventDefault()}
                onDrop={handleDrop}
                className="border-2 border-dashed border-border-light rounded-xl p-xxl text-center hover:border-primary transition-colors cursor-pointer"
                onClick={() => document.getElementById('file-input')?.click()}
              >
                <Upload className="w-12 h-12 text-text-secondary mx-auto mb-m" />
                <p className="text-body-lg text-text-primary mb-s">
                  {file ? file.name : 'Drop your resume here, or click to browse'}
                </p>
                <p className="text-body-sm text-text-secondary">PDF or DOCX, up to 10MB</p>
                <input
                  id="file-input"
                  type="file"
                  accept=".pdf,.docx"
                  onChange={handleFileSelect}
                  className="hidden"
                />
              </div>

              {file && (
                <div className="flex items-center gap-m mt-l p-m bg-gray-50 rounded-lg">
                  <FileText className="w-5 h-5 text-primary" />
                  <span className="text-body-sm flex-1">{file.name}</span>
                  <span className="text-label text-text-secondary">
                    {(file.size / 1024).toFixed(0)} KB
                  </span>
                </div>
              )}

              {error && (
                <p className="text-body-sm text-error mt-m">{error}</p>
              )}

              <div className="mt-l">
                <Button onClick={handleUpload} loading={uploading} disabled={!file}>
                  Upload & Analyze
                </Button>
              </div>
            </div>
          ) : (
            <div className="card p-xl animate-fade-in">
              <div className="flex items-center gap-m mb-l">
                <CheckCircle className="w-6 h-6 text-secondary" />
                <h2 className="text-h2">Resume Analyzed</h2>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-l">
                {preview.name && (
                  <div>
                    <span className="text-label text-text-secondary">Name</span>
                    <p className="text-body-lg mt-xs">{preview.name}</p>
                  </div>
                )}
                {preview.email && (
                  <div>
                    <span className="text-label text-text-secondary">Email</span>
                    <p className="text-body-lg mt-xs">{preview.email}</p>
                  </div>
                )}
                {preview.experience_years != null && (
                  <div>
                    <span className="text-label text-text-secondary">Experience</span>
                    <p className="text-body-lg mt-xs">{preview.experience_years} years</p>
                  </div>
                )}
                {preview.char_count != null && (
                  <div>
                    <span className="text-label text-text-secondary">Resume Length</span>
                    <p className="text-body-lg mt-xs">{preview.char_count.toLocaleString()} characters</p>
                  </div>
                )}
              </div>

              {preview.skills && preview.skills.length > 0 && (
                <div className="mt-l">
                  <span className="text-label text-text-secondary">Skills Detected</span>
                  <div className="flex flex-wrap gap-s mt-s">
                    {preview.skills.map((s) => (
                      <Badge key={s} variant="primary">{s}</Badge>
                    ))}
                  </div>
                </div>
              )}

              {preview.education && preview.education.length > 0 && (
                <div className="mt-l">
                  <span className="text-label text-text-secondary">Education</span>
                  <div className="flex flex-wrap gap-s mt-s">
                    {preview.education.map((e) => (
                      <Badge key={e}>{e}</Badge>
                    ))}
                  </div>
                </div>
              )}

              <div className="mt-xl">
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