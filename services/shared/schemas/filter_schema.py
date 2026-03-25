"""Pydantic schema for Lead Filters (production grade, segmented)."""

from typing import List, Optional

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
