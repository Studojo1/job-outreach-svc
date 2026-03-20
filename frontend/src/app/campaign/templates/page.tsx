'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/hooks/useAuth';
import { useAppStore } from '@/store/useAppStore';
import { Container } from '@/components/layout/Container';
import { Navbar } from '@/components/layout/Navbar';
import { TemplateEditor } from '@/components/features/TemplateEditor';
import { Button } from '@/components/ui/Button';
import { Spinner } from '@/components/ui/Spinner';
import api from '@/lib/api';
import type { EmailTemplate } from '@/lib/types/campaign';

export default function TemplatesPage() {
  const router = useRouter();
  useAuth();
  const { selectedTemplate, setSelectedTemplate } = useAppStore();
  const [templates, setTemplates] = useState<EmailTemplate[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedId, setSelectedId] = useState<number | null>(selectedTemplate?.id || null);

  useEffect(() => {
    api.get('/campaign/templates')
      .then((res) => {
        setTemplates(res.data.templates);
        if (!selectedId && res.data.templates.length > 0) {
          setSelectedId(res.data.templates[0].id);
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [selectedId]);

  const handleSelect = (id: number) => {
    setSelectedId(id);
    const t = templates.find((t) => t.id === id);
    if (t) setSelectedTemplate(t);
  };

  const handleUpdate = (id: number, subject: string, body: string) => {
    setTemplates((prev) =>
      prev.map((t) => (t.id === id ? { ...t, subject, body } : t))
    );
    if (selectedId === id) {
      const t = templates.find((t) => t.id === id);
      if (t) setSelectedTemplate({ ...t, subject, body });
    }
  };

  const handleContinue = () => {
    const t = templates.find((t) => t.id === selectedId);
    if (t) {
      setSelectedTemplate(t);
      router.push('/campaign/setup');
    }
  };

  return (
    <div className="min-h-screen bg-white">
      <Navbar />
      <Container className="max-w-onboarding py-8">
        <div className="mb-8">
          <h1 className="font-clash text-2xl font-bold">Choose an Email Template</h1>
          <p className="text-sm text-muted font-satoshi mt-2">
            Select a template for your outreach emails. You can customize it before sending.
          </p>
        </div>

        {loading ? (
          <div className="flex justify-center py-12"><Spinner /></div>
        ) : (
          <>
            <div className="space-y-4">
              {templates.map((template) => (
                <TemplateEditor
                  key={template.id}
                  template={template}
                  isSelected={selectedId === template.id}
                  onSelect={() => handleSelect(template.id)}
                  onUpdate={(subject, body) => handleUpdate(template.id, subject, body)}
                />
              ))}
            </div>

            <div className="text-center mt-8">
              <Button size="lg" onClick={handleContinue} disabled={!selectedId}>
                Continue to Campaign Setup
              </Button>
            </div>
          </>
        )}
      </Container>
    </div>
  );
}
