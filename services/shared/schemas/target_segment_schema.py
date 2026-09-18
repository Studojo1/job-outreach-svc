"""Pydantic schema for a targeting segment."""

from typing import List

from pydantic import BaseModel


class TargetSegment(BaseModel):
    """A single targeting segment keyed by company size range."""

    company_size_range: str
    person_titles: List[str]
