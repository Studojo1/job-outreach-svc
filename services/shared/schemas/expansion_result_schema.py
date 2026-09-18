"""Pydantic schema for expansion/reduction results."""

from typing import Optional

from pydantic import BaseModel

from services.shared.schemas.filter_schema import LeadFilter


class ExpansionResult(BaseModel):
    """Output of the dynamic filter engine simulation."""

    status: str
    next_stage: Optional[str] = None
    updated_filters: LeadFilter
    message: str
