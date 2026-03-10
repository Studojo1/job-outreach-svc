"""Pydantic schemas for Lead requests and responses."""

from typing import Optional

from pydantic import BaseModel


class LeadCreate(BaseModel):
    """Payload for creating a new lead."""

    campaign_id: str
    name: str
    title: Optional[str] = None
    company: Optional[str] = None
    email: Optional[str] = None
    linkedin_url: Optional[str] = None


class LeadUpdate(BaseModel):
    """Payload for updating an existing lead."""

    name: Optional[str] = None
    title: Optional[str] = None
    company: Optional[str] = None
    email: Optional[str] = None
    linkedin_url: Optional[str] = None
    status: Optional[str] = None


class LeadResponse(BaseModel):
    """Lead data returned to the client."""

    id: str
    campaign_id: str
    name: str
    title: Optional[str] = None
    company: Optional[str] = None
    email: Optional[str] = None
    linkedin_url: Optional[str] = None
    status: str

    model_config = {"from_attributes": True}
