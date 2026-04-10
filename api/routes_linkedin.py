"""LinkedIn outreach routes — token storage, search, leads."""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.dependencies import get_current_user
from database.models import User, LinkedInToken, LinkedInSearchJob, LinkedInOutreachLead
from database.session import get_db
from services.linkedin_outreach.crypto import encrypt_pair, decrypt
from services.linkedin_outreach.voyager import search_people
from services.linkedin_outreach.message_gen import generate_messages_for_leads

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/linkedin", tags=["linkedin"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class SaveTokenRequest(BaseModel):
    li_at: str
    jsessionid: str
    linkedin_name: Optional[str] = None


class TokenStatusResponse(BaseModel):
    connected: bool
    linkedin_name: Optional[str] = None


class StartSearchRequest(BaseModel):
    target_role: str
    target_companies: list[str] = []
    location: Optional[str] = None


class SearchJobResponse(BaseModel):
    id: int
    status: str
    target_role: str
    result_count: int
    error: Optional[str] = None
    created_at: str


class LeadResponse(BaseModel):
    id: int
    name: str
    headline: Optional[str]
    company: Optional[str]
    profile_url: str
    profile_image_url: Optional[str]
    suggested_message: Optional[str]
    message_copied_at: Optional[str]


class MarkCopiedRequest(BaseModel):
    lead_id: int


# ── Token endpoints ────────────────────────────────────────────────────────────

@router.post("/token")
async def save_token(
    body: SaveTokenRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Store encrypted LinkedIn session tokens for the authenticated user.

    Called by the Chrome extension after reading li_at + JSESSIONID cookies.
    """
    if not body.li_at or not body.jsessionid:
        raise HTTPException(status_code=400, detail="li_at and jsessionid are required")

    # Encrypt both tokens with a shared nonce
    li_at_enc, jsessionid_enc, nonce = encrypt_pair(body.li_at, body.jsessionid)

    existing = db.query(LinkedInToken).filter(LinkedInToken.user_id == current_user.id).first()

    if existing:
        existing.li_at_enc = li_at_enc
        existing.jsessionid_enc = jsessionid_enc
        existing.nonce = nonce
        existing.linkedin_name = body.linkedin_name
        existing.updated_at = datetime.utcnow()
    else:
        token_row = LinkedInToken(
            user_id=current_user.id,
            li_at_enc=li_at_enc,
            jsessionid_enc=jsessionid_enc,
            nonce=nonce,
            linkedin_name=body.linkedin_name,
        )
        db.add(token_row)

    db.commit()
    logger.info("Saved LinkedIn token for user %s", current_user.id)
    return {"ok": True}


@router.get("/token/status", response_model=TokenStatusResponse)
async def token_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Check if the user has a stored LinkedIn token."""
    token_row = db.query(LinkedInToken).filter(LinkedInToken.user_id == current_user.id).first()
    if not token_row:
        return TokenStatusResponse(connected=False)
    return TokenStatusResponse(connected=True, linkedin_name=token_row.linkedin_name)


@router.delete("/token")
async def delete_token(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove stored LinkedIn token (disconnect LinkedIn)."""
    db.query(LinkedInToken).filter(LinkedInToken.user_id == current_user.id).delete()
    db.commit()
    return {"ok": True}


# ── Search endpoints ───────────────────────────────────────────────────────────

@router.post("/search", response_model=SearchJobResponse)
async def start_search(
    body: StartSearchRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Start an async people search. Returns a job ID to poll for status/results."""
    token_row = db.query(LinkedInToken).filter(LinkedInToken.user_id == current_user.id).first()
    if not token_row:
        raise HTTPException(status_code=400, detail="LinkedIn not connected. Install the extension first.")

    if not body.target_role.strip():
        raise HTTPException(status_code=400, detail="target_role is required")

    job = LinkedInSearchJob(
        user_id=current_user.id,
        target_role=body.target_role.strip(),
        target_companies=body.target_companies,
        location=body.location,
        status="queued",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    background_tasks.add_task(
        _run_search,
        job_id=job.id,
        user_id=current_user.id,
        user_name=current_user.name,
        li_at_enc=token_row.li_at_enc,
        jsessionid_enc=token_row.jsessionid_enc,
        nonce=token_row.nonce,
        target_role=body.target_role.strip(),
        location=body.location,
    )

    return SearchJobResponse(
        id=job.id,
        status=job.status,
        target_role=job.target_role,
        result_count=0,
        created_at=job.created_at.isoformat(),
    )


@router.get("/search/{job_id}", response_model=SearchJobResponse)
async def get_search_job(
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Poll a search job for status."""
    job = (
        db.query(LinkedInSearchJob)
        .filter(LinkedInSearchJob.id == job_id, LinkedInSearchJob.user_id == current_user.id)
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Search job not found")

    return SearchJobResponse(
        id=job.id,
        status=job.status,
        target_role=job.target_role,
        result_count=job.result_count or 0,
        error=job.error,
        created_at=job.created_at.isoformat(),
    )


@router.get("/search/{job_id}/leads", response_model=list[LeadResponse])
async def get_search_leads(
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get leads for a completed search job."""
    job = (
        db.query(LinkedInSearchJob)
        .filter(LinkedInSearchJob.id == job_id, LinkedInSearchJob.user_id == current_user.id)
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Search job not found")

    leads = (
        db.query(LinkedInOutreachLead)
        .filter(LinkedInOutreachLead.search_job_id == job_id)
        .order_by(LinkedInOutreachLead.id)
        .all()
    )

    return [
        LeadResponse(
            id=l.id,
            name=l.name,
            headline=l.headline,
            company=l.company,
            profile_url=l.profile_url,
            profile_image_url=l.profile_image_url,
            suggested_message=l.suggested_message,
            message_copied_at=l.message_copied_at.isoformat() if l.message_copied_at else None,
        )
        for l in leads
    ]


@router.post("/leads/mark-copied")
async def mark_message_copied(
    body: MarkCopiedRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Track when a student copies a suggested message."""
    lead = (
        db.query(LinkedInOutreachLead)
        .filter(
            LinkedInOutreachLead.id == body.lead_id,
            LinkedInOutreachLead.user_id == current_user.id,
        )
        .first()
    )
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    lead.message_copied_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@router.get("/searches", response_model=list[SearchJobResponse])
async def list_searches(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List recent search jobs for the user (last 10)."""
    jobs = (
        db.query(LinkedInSearchJob)
        .filter(LinkedInSearchJob.user_id == current_user.id)
        .order_by(LinkedInSearchJob.created_at.desc())
        .limit(10)
        .all()
    )
    return [
        SearchJobResponse(
            id=j.id,
            status=j.status,
            target_role=j.target_role,
            result_count=j.result_count or 0,
            error=j.error,
            created_at=j.created_at.isoformat(),
        )
        for j in jobs
    ]


# ── Background task ────────────────────────────────────────────────────────────

async def _run_search(
    job_id: int,
    user_id: str,
    user_name: str,
    li_at_enc: str,
    jsessionid_enc: str,
    nonce: str,
    target_role: str,
    location: Optional[str],
):
    """Background task: run LinkedIn search, generate messages, save leads."""
    from database.session import SessionLocal

    db = SessionLocal()
    try:
        # Mark as running
        job = db.query(LinkedInSearchJob).filter(LinkedInSearchJob.id == job_id).first()
        if not job:
            return
        job.status = "running"
        job.updated_at = datetime.utcnow()
        db.commit()

        # Decrypt tokens
        li_at = decrypt(li_at_enc, nonce)
        jsessionid = decrypt(jsessionid_enc, nonce)

        # Search LinkedIn
        people = await search_people(
            li_at=li_at,
            jsessionid=jsessionid,
            keywords=target_role,
            location=location,
            count=10,
        )

        if not people:
            job.status = "done"
            job.result_count = 0
            job.updated_at = datetime.utcnow()
            db.commit()
            return

        # Generate personalised messages
        people_with_messages = await generate_messages_for_leads(
            leads=people,
            target_role=target_role,
            student_name=user_name,
        )

        # Save leads
        for person in people_with_messages:
            lead = LinkedInOutreachLead(
                search_job_id=job_id,
                user_id=user_id,
                name=person.get("name", ""),
                headline=person.get("headline"),
                company=person.get("company"),
                profile_url=person.get("profile_url", ""),
                profile_image_url=person.get("profile_image_url"),
                suggested_message=person.get("suggested_message"),
            )
            db.add(lead)

        job.status = "done"
        job.result_count = len(people_with_messages)
        job.updated_at = datetime.utcnow()
        db.commit()

        logger.info("Search job %d completed: %d leads", job_id, len(people_with_messages))

    except ValueError as e:
        logger.warning("LinkedIn search job %d failed (user error): %s", job_id, e)
        job = db.query(LinkedInSearchJob).filter(LinkedInSearchJob.id == job_id).first()
        if job:
            job.status = "failed"
            job.error = str(e)
            job.updated_at = datetime.utcnow()
            db.commit()
    except Exception as e:
        logger.error("LinkedIn search job %d failed: %s", job_id, e, exc_info=True)
        job = db.query(LinkedInSearchJob).filter(LinkedInSearchJob.id == job_id).first()
        if job:
            job.status = "failed"
            job.error = "Search failed. Please try again."
            job.updated_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()
