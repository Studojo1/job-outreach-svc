'use client';

import React, { useState } from 'react';
import { ScoreGauge } from '@/components/ui/ScoreGauge';
import { Badge } from '@/components/ui/Badge';
import { MapPin, Building2, Briefcase, ExternalLink } from 'lucide-react';
import type { Lead } from '@/lib/types/lead';

interface FlashCardProps {
  lead: Lead;
}

export function FlashCard({ lead }: FlashCardProps) {
  const [flipped, setFlipped] = useState(false);

  return (
    <div
      className="cursor-pointer"
      style={{ perspective: '1000px', height: '260px' }}
      onMouseEnter={() => setFlipped(true)}
      onMouseLeave={() => setFlipped(false)}
    >
      <div
        className="relative w-full h-full transition-transform duration-500 ease-in-out"
        style={{
          transformStyle: 'preserve-3d',
          transform: flipped ? 'rotateY(180deg)' : 'rotateY(0deg)',
        }}
      >
        {/* Front */}
        <div
          className="absolute inset-0 bg-card border border-border-light rounded-xl shadow-soft px-4 py-3 flex flex-col justify-between"
          style={{ backfaceVisibility: 'hidden' }}
        >
          <div>
            <div className="flex items-start justify-between mb-2">
              <div className="min-w-0 flex-1 mr-2">
                <h3 className="text-sm font-semibold text-text-primary truncate">{lead.name}</h3>
                <p className="text-xs text-text-secondary mt-0.5 truncate">{lead.title}</p>
              </div>
              {lead.score && <ScoreGauge score={lead.score.overall} size={36} />}
            </div>
            <div className="flex flex-col gap-1 text-xs text-text-secondary">
              <div className="flex items-center gap-1.5">
                <Building2 className="w-3 h-3 flex-shrink-0" />
                <span className="truncate">{lead.company}</span>
              </div>
              {lead.location && (
                <div className="flex items-center gap-1.5">
                  <MapPin className="w-3 h-3 flex-shrink-0" />
                  <span className="truncate">{lead.location}</span>
                </div>
              )}
              {lead.industry && (
                <div className="flex items-center gap-1.5">
                  <Briefcase className="w-3 h-3 flex-shrink-0" />
                  <span className="truncate">{lead.industry}</span>
                </div>
              )}
            </div>
          </div>
          <div className="flex items-center justify-between mt-2">
            <Badge variant={lead.email_verified ? 'success' : 'default'}>
              {lead.email_verified ? 'Verified' : lead.status}
            </Badge>
            <span className="text-[10px] text-text-secondary">Hover to flip</span>
          </div>
        </div>

        {/* Back */}
        <div
          className="absolute inset-0 bg-card border border-primary rounded-xl shadow-elevated px-4 py-3 flex flex-col justify-between"
          style={{ backfaceVisibility: 'hidden', transform: 'rotateY(180deg)' }}
        >
          <div>
            <h4 className="text-sm font-semibold text-primary mb-2">Score Breakdown</h4>
            {lead.score ? (
              <div className="space-y-1.5">
                <ScoreBar label="Title" value={lead.score.title_relevance} max={35} />
                <ScoreBar label="Dept" value={lead.score.department_relevance} max={20} />
                <ScoreBar label="Industry" value={lead.score.industry_relevance} max={15} />
                <ScoreBar label="Seniority" value={lead.score.seniority_relevance} max={10} />
                <ScoreBar label="Location" value={lead.score.location_relevance} max={10} />
              </div>
            ) : (
              <p className="text-xs text-text-secondary">No score data</p>
            )}
            {lead.score?.explanation && (
              <p className="text-xs text-text-secondary mt-2 line-clamp-2">{lead.score.explanation}</p>
            )}
          </div>
          {lead.linkedin_url && (
            <a
              href={lead.linkedin_url}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1 text-primary text-xs hover:underline mt-2"
              onClick={(e) => e.stopPropagation()}
            >
              <ExternalLink className="w-3 h-3" /> LinkedIn
            </a>
          )}
        </div>
      </div>
    </div>
  );
}

function ScoreBar({ label, value, max }: { label: string; value: number; max: number }) {
  const pct = Math.min((value / max) * 100, 100);
  return (
    <div>
      <div className="flex justify-between text-xs mb-0.5">
        <span className="text-text-secondary">{label}</span>
        <span className="text-text-primary font-medium">{value}/{max}</span>
      </div>
      <div className="w-full h-1.5 bg-gray-100 rounded-full">
        <div className="h-full bg-primary rounded-full transition-all duration-500" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}