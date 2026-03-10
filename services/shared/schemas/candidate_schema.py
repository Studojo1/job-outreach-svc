"""Pydantic schema for Candidate profiles."""

from typing import Dict, List, Optional

from pydantic import BaseModel


class CandidateProfile(BaseModel):
    """Rich profile of a candidate seeking opportunities."""

    user_id: str
    name: str

    education: Optional[Dict] = None

    location_preferences: List[str]

    skills: List[str]

    tools: Optional[List[str]] = None

    experience_level: str

    preferred_roles: List[str]

    role_seniority_target: List[str]

    company_preferences: Dict
    # Expected keys inside company_preferences:
    #   company_stage: list[str]   e.g. ["startup", "growth"]
    #   company_size:  list[str]   e.g. ["1,50", "51,200"]
    #   industries:    list[str]   e.g. ["fintech", "saas"]

    work_preferences: Optional[Dict] = None
