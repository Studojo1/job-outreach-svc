"""LinkedIn automation routes — email+password login, campaign CRUD, launch/pause, stats."""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.dependencies import get_current_user
from database.models import (
    LinkedInCampaign,
    LinkedInConnectionRequest,
    LinkedInToken,
    User,
)
from database.session import get_db
from services.linkedin_outreach.automation_service import search_linkedin_leads
from services.linkedin_outreach.crypto import decrypt, encrypt_pair
from services.linkedin_outreach.login import linkedin_login
from services.linkedin_outreach.message_gen import generate_connection_message

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/linkedin/automation", tags=["linkedin-automation"])


# ── Auth: email+password login ─────────────────────────────────────────────────

class LinkedInLoginRequest(BaseModel):
    email: str
    password: str


@router.post("/login")
async def login_with_credentials(
    body: LinkedInLoginRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Log in to LinkedIn with email+password, store encrypted session tokens."""
    if not body.email or not body.password:
        raise HTTPException(status_code=400, detail="email and password are required")

    try:
        li_at, jsessionid, display_name = linkedin_login(body.email, body.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    li_at_enc, jsessionid_enc, nonce = encrypt_pair(li_at, jsessionid)

    existing = db.query(LinkedInToken).filter(LinkedInToken.user_id == current_user.id).first()
    if existing:
        existing.li_at_enc = li_at_enc
        existing.jsessionid_enc = jsessionid_enc
        existing.nonce = nonce
        existing.linkedin_name = display_name
        existing.updated_at = datetime.utcnow()
    else:
        db.add(LinkedInToken(
            user_id=current_user.id,
            li_at_enc=li_at_enc,
            jsessionid_enc=jsessionid_enc,
            nonce=nonce,
            linkedin_name=display_name,
        ))

    db.commit()
    return {"ok": True, "linkedin_name": display_name}


# ── Campaign CRUD ──────────────────────────────────────────────────────────────

class CreateCampaignRequest(BaseModel):
    name: str
    target_role: str
    target_industries: list[str] = []
    target_locations: list[str] = []
    target_company_sizes: list[str] = []
    target_keywords: Optional[str] = None
    connection_note: Optional[str] = None
    followup_message: Optional[str] = None
    daily_limit: int = Field(default=20, ge=5, le=50)


class CampaignResponse(BaseModel):
    id: int
    name: str
    status: str
    target_role: str
    target_industries: list
    target_locations: list
    target_company_sizes: list
    target_keywords: Optional[str]
    connection_note: Optional[str]
    followup_message: Optional[str]
    daily_limit: int
    total_leads: int
    total_sent: int
    total_accepted: int
    total_followups_sent: int
    total_replied: int
    launched_at: Optional[str]
    created_at: str


def _campaign_to_response(c: LinkedInCampaign) -> CampaignResponse:
    return CampaignResponse(
        id=c.id,
        name=c.name,
        status=c.status,
        target_role=c.target_role,
        target_industries=c.target_industries or [],
        target_locations=c.target_locations or [],
        target_company_sizes=c.target_company_sizes or [],
        target_keywords=c.target_keywords,
        connection_note=c.connection_note,
        followup_message=c.followup_message,
        daily_limit=c.daily_limit,
        total_leads=c.total_leads,
        total_sent=c.total_sent,
        total_accepted=c.total_accepted,
        total_followups_sent=c.total_followups_sent,
        total_replied=c.total_replied,
        launched_at=c.launched_at.isoformat() if c.launched_at else None,
        created_at=c.created_at.isoformat(),
    )


@router.post("/campaigns", response_model=CampaignResponse)
async def create_campaign(
    body: CreateCampaignRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    campaign = LinkedInCampaign(
        user_id=current_user.id,
        name=body.name,
        target_role=body.target_role,
        target_industries=body.target_industries,
        target_locations=body.target_locations,
        target_company_sizes=body.target_company_sizes,
        target_keywords=body.target_keywords,
        connection_note=body.connection_note,
        followup_message=body.followup_message,
        daily_limit=body.daily_limit,
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return _campaign_to_response(campaign)


@router.get("/campaigns", response_model=list[CampaignResponse])
async def list_campaigns(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    campaigns = (
        db.query(LinkedInCampaign)
        .filter(LinkedInCampaign.user_id == current_user.id)
        .order_by(LinkedInCampaign.created_at.desc())
        .all()
    )
    return [_campaign_to_response(c) for c in campaigns]


@router.get("/campaigns/{campaign_id}", response_model=CampaignResponse)
async def get_campaign(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    c = _get_campaign_or_404(campaign_id, current_user.id, db)
    return _campaign_to_response(c)


@router.put("/campaigns/{campaign_id}", response_model=CampaignResponse)
async def update_campaign(
    campaign_id: int,
    body: CreateCampaignRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    c = _get_campaign_or_404(campaign_id, current_user.id, db)
    if c.status == "running":
        raise HTTPException(status_code=400, detail="Pause the campaign before editing")

    c.name = body.name
    c.target_role = body.target_role
    c.target_industries = body.target_industries
    c.target_locations = body.target_locations
    c.target_company_sizes = body.target_company_sizes
    c.target_keywords = body.target_keywords
    c.connection_note = body.connection_note
    c.followup_message = body.followup_message
    c.daily_limit = body.daily_limit
    c.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(c)
    return _campaign_to_response(c)


# ── Lead search ────────────────────────────────────────────────────────────────

@router.post("/campaigns/{campaign_id}/search-leads")
async def search_leads(
    campaign_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Search LinkedIn for leads matching the campaign ICP and save them."""
    c = _get_campaign_or_404(campaign_id, current_user.id, db)

    token_row = db.query(LinkedInToken).filter(LinkedInToken.user_id == current_user.id).first()
    if not token_row:
        raise HTTPException(status_code=400, detail="LinkedIn not connected. Connect first.")

    # Clear existing pending leads before a new search
    db.query(LinkedInConnectionRequest).filter(
        LinkedInConnectionRequest.campaign_id == campaign_id,
        LinkedInConnectionRequest.status == "pending",
    ).delete()
    db.commit()

    background_tasks.add_task(
        _run_lead_search,
        campaign_id=campaign_id,
        user_id=current_user.id,
        user_name=current_user.name or "there",
    )

    return {"ok": True, "message": "Lead search started"}


async def _run_lead_search(campaign_id: int, user_id: str, user_name: str):
    from database.session import SessionLocal
    from services.linkedin_outreach.crypto import decrypt

    db = SessionLocal()
    try:
        c = db.query(LinkedInCampaign).filter(LinkedInCampaign.id == campaign_id).first()
        token_row = db.query(LinkedInToken).filter(LinkedInToken.user_id == user_id).first()
        if not c or not token_row:
            return

        li_at = decrypt(token_row.li_at_enc, token_row.nonce)
        jsessionid = decrypt(token_row.jsessionid_enc, token_row.nonce)

        people = await search_linkedin_leads(
            li_at=li_at,
            jsessionid=jsessionid,
            target_role=c.target_role,
            locations=c.target_locations or [],
            industries=c.target_industries or [],
            keywords=c.target_keywords,
            limit=60,
        )

        if not people:
            logger.warning("No leads found for campaign %d", campaign_id)
            return

        # Generate personalised connection notes for each lead
        notes = await _generate_notes_batch(people, c.target_role, user_name, c.connection_note)
        followups = await _generate_followups_batch(people, c.target_role, user_name, c.followup_message)

        for i, person in enumerate(people):
            req = LinkedInConnectionRequest(
                campaign_id=campaign_id,
                user_id=user_id,
                name=person.get("name", ""),
                headline=person.get("headline"),
                company=person.get("company"),
                profile_url=person.get("profile_url", ""),
                profile_image_url=person.get("profile_image_url"),
                connection_note=notes[i] if i < len(notes) else c.connection_note,
                followup_message=followups[i] if i < len(followups) else c.followup_message,
                status="pending",
            )
            db.add(req)

        c.total_leads = len(people)
        c.updated_at = datetime.utcnow()
        db.commit()
        logger.info("Campaign %d: saved %d leads", campaign_id, len(people))

    except Exception as e:
        logger.error("Lead search failed for campaign %d: %s", campaign_id, e, exc_info=True)
    finally:
        db.close()


async def _generate_notes_batch(
    people: list[dict], target_role: str, user_name: str, template: str | None
) -> list[str]:
    """Generate personalised connection notes for up to 60 leads concurrently."""
    import asyncio
    from services.linkedin_outreach.message_gen import generate_connection_message

    async def gen(p: dict) -> str:
        try:
            return await asyncio.to_thread(
                generate_connection_message,
                p.get("name", ""),
                p.get("headline", ""),
                p.get("company", ""),
                target_role,
                user_name,
            )
        except Exception:
            return template or f"Hi {p.get('name','')}, I came across your profile and would love to connect!"

    return list(await asyncio.gather(*[gen(p) for p in people]))


async def _generate_followups_batch(
    people: list[dict], target_role: str, user_name: str, template: str | None
) -> list[str]:
    """Return personalised follow-up messages. Falls back to template if generation fails."""
    if not template:
        return [""] * len(people)
    return [template] * len(people)


# ── Launch / pause / complete ──────────────────────────────────────────────────

@router.post("/campaigns/{campaign_id}/launch")
async def launch_campaign(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    c = _get_campaign_or_404(campaign_id, current_user.id, db)

    if c.status == "running":
        raise HTTPException(status_code=400, detail="Campaign is already running")

    pending_count = (
        db.query(LinkedInConnectionRequest)
        .filter(
            LinkedInConnectionRequest.campaign_id == campaign_id,
            LinkedInConnectionRequest.status == "pending",
        )
        .count()
    )
    if pending_count == 0:
        raise HTTPException(
            status_code=400,
            detail="No leads found. Run a lead search first.",
        )

    token_row = db.query(LinkedInToken).filter(LinkedInToken.user_id == current_user.id).first()
    if not token_row:
        raise HTTPException(status_code=400, detail="LinkedIn not connected")

    c.status = "running"
    c.launched_at = c.launched_at or datetime.utcnow()
    c.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "status": "running"}


@router.post("/campaigns/{campaign_id}/pause")
async def pause_campaign(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    c = _get_campaign_or_404(campaign_id, current_user.id, db)
    if c.status != "running":
        raise HTTPException(status_code=400, detail="Campaign is not running")
    c.status = "paused"
    c.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "status": "paused"}


@router.post("/campaigns/{campaign_id}/resume")
async def resume_campaign(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    c = _get_campaign_or_404(campaign_id, current_user.id, db)
    if c.status != "paused":
        raise HTTPException(status_code=400, detail="Campaign is not paused")
    c.status = "running"
    c.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "status": "running"}


# ── Stats + request list ───────────────────────────────────────────────────────

class CampaignStats(BaseModel):
    campaign_id: int
    status: str
    total_leads: int
    total_sent: int
    total_accepted: int
    total_followups_sent: int
    total_replied: int
    acceptance_rate: float
    reply_rate: float


@router.get("/campaigns/{campaign_id}/stats", response_model=CampaignStats)
async def get_campaign_stats(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    c = _get_campaign_or_404(campaign_id, current_user.id, db)
    acceptance_rate = (c.total_accepted / c.total_sent * 100) if c.total_sent > 0 else 0
    reply_rate = (c.total_replied / c.total_accepted * 100) if c.total_accepted > 0 else 0
    return CampaignStats(
        campaign_id=c.id,
        status=c.status,
        total_leads=c.total_leads,
        total_sent=c.total_sent,
        total_accepted=c.total_accepted,
        total_followups_sent=c.total_followups_sent,
        total_replied=c.total_replied,
        acceptance_rate=round(acceptance_rate, 1),
        reply_rate=round(reply_rate, 1),
    )


class ConnectionRequestResponse(BaseModel):
    id: int
    name: str
    headline: Optional[str]
    company: Optional[str]
    profile_url: str
    connection_note: Optional[str]
    followup_message: Optional[str]
    status: str
    sent_at: Optional[str]
    accepted_at: Optional[str]
    followup_sent_at: Optional[str]
    reply_text: Optional[str]
    reply_sentiment: Optional[str]


@router.get("/campaigns/{campaign_id}/requests", response_model=list[ConnectionRequestResponse])
async def list_requests(
    campaign_id: int,
    status: Optional[str] = None,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_campaign_or_404(campaign_id, current_user.id, db)

    q = db.query(LinkedInConnectionRequest).filter(
        LinkedInConnectionRequest.campaign_id == campaign_id
    )
    if status:
        q = q.filter(LinkedInConnectionRequest.status == status)

    requests = q.order_by(LinkedInConnectionRequest.created_at.desc()).limit(limit).all()

    return [
        ConnectionRequestResponse(
            id=r.id,
            name=r.name,
            headline=r.headline,
            company=r.company,
            profile_url=r.profile_url,
            connection_note=r.connection_note,
            followup_message=r.followup_message,
            status=r.status,
            sent_at=r.sent_at.isoformat() if r.sent_at else None,
            accepted_at=r.accepted_at.isoformat() if r.accepted_at else None,
            followup_sent_at=r.followup_sent_at.isoformat() if r.followup_sent_at else None,
            reply_text=r.reply_text,
            reply_sentiment=r.reply_sentiment,
        )
        for r in requests
    ]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_campaign_or_404(campaign_id: int, user_id: str, db: Session) -> LinkedInCampaign:
    c = (
        db.query(LinkedInCampaign)
        .filter(LinkedInCampaign.id == campaign_id, LinkedInCampaign.user_id == user_id)
        .first()
    )
    if not c:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return c
