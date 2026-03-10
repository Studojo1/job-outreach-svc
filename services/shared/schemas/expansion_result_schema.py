"""Pydantic schema for expansion/reduction results."""

from typing import Optional

from pydantic import BaseModel

from job_outreach_tool.services.shared.schemas.filter_schema import LeadFilter


class ExpansionResult(BaseModel):
    """Output of the dynamic filter engine simulation."""

    status: str
    next_stage: Optional[str] = None
    updated_filters: LeadFilter
    message: str
