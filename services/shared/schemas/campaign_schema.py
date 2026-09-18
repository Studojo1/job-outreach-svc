"""Pydantic schemas for Campaign requests and responses."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CampaignCreate(BaseModel):
    """Payload for creating a new campaign."""

    user_id: str
    roles: str = Field(..., description="Comma-separated target roles")
    location: str
    target_leads: int = 50


class CampaignUpdate(BaseModel):
    """Payload for updating an existing campaign."""

    roles: Optional[str] = None
    location: Optional[str] = None
    target_leads: Optional[int] = None
    status: Optional[str] = None


class CampaignResponse(BaseModel):
    """Campaign data returned to the client."""

    id: str
    user_id: str
    roles: str
    location: str
    target_leads: int
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}
