'use client';

import React, { useState } from 'react';
import { Button } from '@/components/ui/Button';
import { Pencil, Check } from 'lucide-react';
import type { EmailTemplate } from '@/lib/types/campaign';

interface TemplateEditorProps {
  template: EmailTemplate;
  isSelected: boolean;
  onSelect: () => void;
  onUpdate: (subject: string, body: string) => void;
}

export function TemplateEditor({ template, isSelected, onSelect, onUpdate }: TemplateEditorProps) {
  const [editing, setEditing] = useState(false);
  const [subject, setSubject] = useState(template.subject);
  const [body, setBody] = useState(template.body);

  const handleSave = () => {
    onUpdate(subject, body);
    setEditing(false);
  };

  return (
    <div
      className={`border-2 rounded-xl p-l transition-all duration-200 cursor-pointer
        ${isSelected ? 'border-primary bg-primary/5 shadow-elevated' : 'border-border-light hover:border-gray-300'}`}
      onClick={() => !editing && onSelect()}
    >
      <div className="flex items-center justify-between mb-m">
        <h4 className="text-h3">{template.name}</h4>
        <div className="flex gap-s">
          {isSelected && !editing && (
            <button onClick={(e) => { e.stopPropagation(); setEditing(true); }}
                    className="text-text-secondary hover:text-primary transition-colors">
              <Pencil className="w-4 h-4" />
            </button>
          )}
          {isSelected && <Check className="w-5 h-5 text-primary" />}
        </div>
      </div>

      {editing ? (
        <div className="space-y-m" onClick={(e) => e.stopPropagation()}>
          <div>
            <label className="text-label text-text-secondary block mb-xs">Subject</label>
            <input value={subject} onChange={(e) => setSubject(e.target.value)}
                   className="w-full px-m py-2 border border-border-light rounded-md text-body-sm focus:outline-none focus:ring-2 focus:ring-primary" />
          </div>
          <div>
            <label className="text-label text-text-secondary block mb-xs">Body</label>
            <textarea value={body} onChange={(e) => setBody(e.target.value)} rows={6}
                      className="w-full px-m py-2 border border-border-light rounded-md text-body-sm focus:outline-none focus:ring-2 focus:ring-primary resize-none" />
          </div>
          <div className="flex gap-s">
            <Button size="sm" onClick={handleSave}>Save</Button>
            <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>Cancel</Button>
          </div>
        </div>
      ) : (
        <div>
          <p className="text-body-sm text-text-secondary font-medium">Subject: {subject}</p>
          <p className="text-body-sm text-text-secondary mt-s line-clamp-3">{body}</p>
        </div>
      )}

      {isSelected && (
        <div className="flex gap-xs mt-m flex-wrap">
          {['{name}', '{company}', '{title}'].map((v) => (
            <span key={v} className="px-2 py-1 bg-primary/10 text-primary rounded text-label">{v}</span>
          ))}
        </div>
      )}
    </div>
  );
}
