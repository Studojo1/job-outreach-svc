"""Pydantic schema for Lead Filters (production grade, segmented).

Fields added in May 2026 after the Phase A Apollo audit:
  q_organization_job_titles               — companies currently hiring for this role
  organization_job_posted_at_range        — recency window for the postings
  q_organization_keyword_tags             — niche tags (OR'd, up to 3)
  currently_using_any_of_technology_uids  — tech stack (any-of)
  organization_job_locations              — job posting location (vs HQ)
  person_past_titles                      — managers who came up through the role
"""

from typing import List, Optional, Dict

from pydantic import BaseModel

from services.shared.schemas.target_segment_schema import TargetSegment


class LeadFilter(BaseModel):
    """Production-grade segmented filters for Apollo lead search."""

    target_segments: List[TargetSegment]
    person_titles_exclude: Optional[List[str]] = None
    person_locations: List[str]
    organization_locations: Optional[List[str]] = None
    organization_industries: Optional[List[str]] = None
    q_organization_name: Optional[str] = None
    email_status: Optional[List[str]] = None

    # ── Phase A audit-passed additions (May 2026) ─────────────────────────
    q_organization_job_titles: Optional[List[str]] = None
    organization_job_posted_at_range: Optional[Dict[str, str]] = None
    q_organization_keyword_tags: Optional[List[str]] = None
    currently_using_any_of_technology_uids: Optional[List[str]] = None
    organization_job_locations: Optional[List[str]] = None
    person_past_titles: Optional[List[str]] = None
