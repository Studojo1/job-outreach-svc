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
      className={`border-2 rounded-2xl p-6 transition-all duration-200 cursor-pointer
        ${isSelected ? 'border-primary bg-primary/5 shadow-brutal-active' : 'border-ink/20 hover:border-ink hover:shadow-brutal-active'}`}
      onClick={() => !editing && onSelect()}
    >
      <div className="flex items-center justify-between mb-4">
        <h4 className="font-clash text-lg font-bold">{template.name}</h4>
        <div className="flex gap-2">
          {isSelected && !editing && (
            <button onClick={(e) => { e.stopPropagation(); setEditing(true); }}
                    className="text-muted hover:text-primary transition-colors">
              <Pencil className="w-4 h-4" />
            </button>
          )}
          {isSelected && <Check className="w-5 h-5 text-primary" />}
        </div>
      </div>

      {editing ? (
        <div className="space-y-4" onClick={(e) => e.stopPropagation()}>
          <div>
            <label className="text-xs font-bold text-muted uppercase font-satoshi block mb-1">Subject</label>
            <input value={subject} onChange={(e) => setSubject(e.target.value)}
                   className="w-full px-4 py-2 border-2 border-ink/20 rounded-xl text-sm font-satoshi focus:outline-none focus:ring-2 focus:ring-primary" />
          </div>
          <div>
            <label className="text-xs font-bold text-muted uppercase font-satoshi block mb-1">Body</label>
            <textarea value={body} onChange={(e) => setBody(e.target.value)} rows={6}
                      className="w-full px-4 py-2 border-2 border-ink/20 rounded-xl text-sm font-satoshi focus:outline-none focus:ring-2 focus:ring-primary resize-none" />
          </div>
          <div className="flex gap-2">
            <Button size="sm" onClick={handleSave}>Save</Button>
            <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>Cancel</Button>
          </div>
        </div>
      ) : (
        <div>
          <p className="text-sm text-muted font-bold font-satoshi">Subject: {subject}</p>
          <p className="text-sm text-muted font-satoshi mt-2 line-clamp-3">{body}</p>
        </div>
      )}

      {isSelected && (
        <div className="flex gap-1 mt-4 flex-wrap">
          {['{name}', '{company}', '{title}'].map((v) => (
            <span key={v} className="px-2 py-1 bg-primary/10 text-primary rounded text-xs font-bold uppercase font-satoshi">{v}</span>
          ))}
        </div>
      )}
    </div>
  );
}
