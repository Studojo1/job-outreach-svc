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
from services.linkedin_outreach.login import linkedin_check_phone_tap, linkedin_login_start, linkedin_verify_pin
from services.linkedin_outreach.message_gen import generate_connection_message

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/linkedin/automation", tags=["linkedin-automation"])


# ── Auth: email+password login ─────────────────────────────────────────────────

class LinkedInLoginRequest(BaseModel):
    email: str
    password: str


class LinkedInVerifyPinRequest(BaseModel):
    session_key: str
    pin: str


class LinkedInCheckPhoneTapRequest(BaseModel):
    session_key: str


def _store_token(db, user_id: str, li_at: str, jsessionid: str, display_name: str | None):
    import secrets as _secrets
    li_at_enc, jsessionid_enc, nonce = encrypt_pair(li_at, jsessionid)
    proxy_session_id = _secrets.token_hex(8)

    existing = db.query(LinkedInToken).filter(LinkedInToken.user_id == user_id).first()
    if existing:
        existing.li_at_enc = li_at_enc
        existing.jsessionid_enc = jsessionid_enc
        existing.nonce = nonce
        existing.linkedin_name = display_name
        existing.proxy_session_id = proxy_session_id
        existing.updated_at = datetime.utcnow()
    else:
        db.add(LinkedInToken(
            user_id=user_id,
            li_at_enc=li_at_enc,
            jsessionid_enc=jsessionid_enc,
            nonce=nonce,
            linkedin_name=display_name,
            proxy_session_id=proxy_session_id,
        ))
    db.commit()
    return display_name


@router.post("/login")
async def login_with_credentials(
    body: LinkedInLoginRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Step 1 — attempt login. Returns ok=True on success, or challenge_required=True + session_key if PIN needed."""
    if not body.email or not body.password:
        raise HTTPException(status_code=400, detail="email and password are required")

    try:
        from core.config import settings as _s
        proxy_url = (_s.LINKEDIN_PROXY_URL or "").strip() or None
        li_at, jsessionid, display_name, session_key = await linkedin_login_start(
            body.email, body.password, proxy_url=proxy_url
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if li_at is None:
        from services.linkedin_outreach.login import _pending
        challenge_type = _pending.get(session_key, {}).get("challenge_type", "pin")
        return {"ok": False, "challenge_required": True, "session_key": session_key, "challenge_type": challenge_type}

    _store_token(db, current_user.id, li_at, jsessionid, display_name)
    return {"ok": True, "linkedin_name": display_name}


@router.post("/login/verify-pin")
async def verify_pin(
    body: LinkedInVerifyPinRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Step 2 — submit the PIN LinkedIn emailed. Completes login and stores the session."""
    if not body.session_key or not body.pin:
        raise HTTPException(status_code=400, detail="session_key and pin are required")

    try:
        li_at, jsessionid, display_name = await linkedin_verify_pin(body.session_key, body.pin)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    _store_token(db, current_user.id, li_at, jsessionid, display_name)
    return {"ok": True, "linkedin_name": display_name}


@router.post("/login/check-phone-tap")
async def check_phone_tap(
    body: LinkedInCheckPhoneTapRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Poll whether the user approved the phone notification. Returns ok=True when approved."""
    if not body.session_key:
        raise HTTPException(status_code=400, detail="session_key is required")
    try:
        result = await linkedin_check_phone_tap(body.session_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result is None:
        return {"ok": False, "still_waiting": True}
    li_at, jsessionid, display_name = result
    _store_token(db, current_user.id, li_at, jsessionid, display_name)
    return {"ok": True, "linkedin_name": display_name}


class LinkedInCookieLoginRequest(BaseModel):
    li_at: str
    jsessionid: str


@router.post("/login/cookies")
async def login_with_cookies(
    body: LinkedInCookieLoginRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Store LinkedIn session cookies directly (bypasses email+password challenge)."""
    if not body.li_at or not body.jsessionid:
        raise HTTPException(status_code=400, detail="li_at and jsessionid are required")

    display_name = None
    try:
        import re as _re
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            res = await client.get(
                "https://www.linkedin.com/in/me/",
                headers={
                    "Cookie": f"li_at={body.li_at}; JSESSIONID={body.jsessionid}",
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
                    ),
                },
            )
        match = _re.search(r"<title>([^|<]+)", res.text)
        if match:
            display_name = match.group(1).strip()
        if not display_name or "Sign In" in (display_name or ""):
            raise HTTPException(status_code=400, detail="Cookies are invalid or expired. Please copy fresh cookies from your browser.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not validate cookies: {e}")

    _store_token(db, current_user.id, body.li_at, body.jsessionid.strip('"'), display_name)
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

    # Clear existing pending leads and reset search_failed status before a new search
    db.query(LinkedInConnectionRequest).filter(
        LinkedInConnectionRequest.campaign_id == campaign_id,
        LinkedInConnectionRequest.status == "pending",
    ).delete()
    if c.status == "search_failed":
        c.status = "draft"
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

        logger.info(
            "Starting lead search for campaign %d: role=%r locations=%r industries=%r keywords=%r",
            campaign_id, c.target_role, c.target_locations, c.target_industries, c.target_keywords,
        )

        people = await search_linkedin_leads(
            li_at=li_at,
            jsessionid=jsessionid,
            target_role=c.target_role,
            locations=c.target_locations or [],
            industries=c.target_industries or [],
            keywords=c.target_keywords,
            limit=30,
        )

        logger.info("Lead search for campaign %d returned %d people", campaign_id, len(people))

        if not people:
            logger.warning("No leads found for campaign %d — check search params and Apollo key", campaign_id)
            c.status = "search_failed"
            c.updated_at = datetime.utcnow()
            db.commit()
            return

        # Save leads immediately so frontend sees them fast — use template notes for now.
        # AI personalisation runs in a background task after the DB commit.
        template_note = c.connection_note or ""
        template_followup = c.followup_message or ""

        req_ids = []
        for person in people:
            req = LinkedInConnectionRequest(
                campaign_id=campaign_id,
                user_id=user_id,
                name=person.get("name", ""),
                headline=person.get("headline"),
                company=person.get("company"),
                profile_url=person.get("profile_url", ""),
                profile_image_url=person.get("profile_image_url"),
                connection_note=template_note,
                followup_message=template_followup,
                status="pending",
            )
            db.add(req)

        c.total_leads = len(people)
        c.updated_at = datetime.utcnow()
        db.commit()
        logger.info("Campaign %d: saved %d leads (template notes)", campaign_id, len(people))

        # Refresh req IDs then personalise in background
        reqs = db.query(LinkedInConnectionRequest).filter(
            LinkedInConnectionRequest.campaign_id == campaign_id,
            LinkedInConnectionRequest.status == "pending",
        ).all()
        req_ids = [r.id for r in reqs]

        # Fire-and-forget AI personalisation — does not block lead availability
        import asyncio as _asyncio
        _asyncio.create_task(_personalise_leads(req_ids, people, c.target_role, user_name, c.connection_note))

    except Exception as e:
        logger.error("Lead search failed for campaign %d: %s", campaign_id, e, exc_info=True)
    finally:
        db.close()


async def _personalise_leads(
    req_ids: list[int], people: list[dict], target_role: str, user_name: str, template: str | None
) -> None:
    """Background task: update saved leads with AI-personalised connection notes."""
    from database.session import SessionLocal
    from services.linkedin_outreach.message_gen import generate_connection_message

    db = SessionLocal()
    try:
        for req_id, person in zip(req_ids, people):
            try:
                note = await generate_connection_message(
                    person_name=person.get("name", ""),
                    person_headline=person.get("headline", ""),
                    person_company=person.get("company", ""),
                    target_role=target_role,
                    student_name=user_name,
                )
                req = db.query(LinkedInConnectionRequest).filter(LinkedInConnectionRequest.id == req_id).first()
                if req and req.status == "pending":
                    req.connection_note = note
            except Exception:
                pass
        db.commit()
        logger.info("Personalised %d connection notes in background", len(req_ids))
    except Exception as e:
        logger.error("Background personalisation failed: %s", e)
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


@router.post("/campaigns/{campaign_id}/send-one")
async def send_one_now(
    campaign_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Immediately attempt to send one pending connection request, bypassing the daemon sleep."""
    from services.linkedin_outreach.automation_service import (
        resolve_profile_urn, send_connection_request, LinkedInAuthError,
    )

    c = _get_campaign_or_404(campaign_id, current_user.id, db)
    if c.status != "running":
        raise HTTPException(status_code=400, detail="Campaign is not running")

    token_row = db.query(LinkedInToken).filter(LinkedInToken.user_id == current_user.id).first()
    if not token_row:
        raise HTTPException(status_code=400, detail="No LinkedIn token found — reconnect first")

    try:
        li_at = decrypt(token_row.li_at_enc, token_row.nonce)
        jsessionid = decrypt(token_row.jsessionid_enc, token_row.nonce)
    except Exception:
        c.status = "auth_failed"
        c.updated_at = datetime.utcnow()
        db.commit()
        raise HTTPException(status_code=400, detail="Token decryption failed — reconnect LinkedIn")

    req = (
        db.query(LinkedInConnectionRequest)
        .filter(
            LinkedInConnectionRequest.campaign_id == campaign_id,
            LinkedInConnectionRequest.status == "pending",
        )
        .first()
    )
    if not req:
        raise HTTPException(status_code=404, detail="No pending leads to send")

    # Resolve URN
    if not req.profile_urn:
        try:
            urn = await resolve_profile_urn(li_at, jsessionid, req.profile_url, token_row.proxy_session_id)
        except LinkedInAuthError:
            c.status = "auth_failed"
            c.updated_at = datetime.utcnow()
            db.commit()
            raise HTTPException(status_code=401, detail="LinkedIn session expired — reconnect")
        if not urn:
            req.status = "error"
            req.error = "Could not resolve profile URN"
            req.updated_at = datetime.utcnow()
            db.commit()
            raise HTTPException(status_code=422, detail=f"Could not resolve URN for {req.profile_url}")
        req.profile_urn = urn

    # Send
    try:
        ok = await send_connection_request(
            li_at, jsessionid, req.profile_urn, req.connection_note or "", token_row.proxy_session_id
        )
    except LinkedInAuthError:
        c.status = "auth_failed"
        c.updated_at = datetime.utcnow()
        db.commit()
        raise HTTPException(status_code=401, detail="LinkedIn session expired — reconnect")

    if ok:
        req.status = "sent"
        req.sent_at = datetime.utcnow()
        c.total_sent += 1
        result = "sent"
    else:
        req.status = "error"
        req.error = "Send failed"
        result = "error"

    req.updated_at = datetime.utcnow()
    c.updated_at = datetime.utcnow()
    db.commit()

    return {"ok": ok, "result": result, "lead_name": req.name, "profile_url": req.profile_url}


@router.post("/campaigns/{campaign_id}/retry-errors")
async def retry_errors(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Reset errored leads back to pending so the daemon retries them.

    Only resets leads that already have a resolved profile_urn — i.e. those
    that failed at the connection POST step, not at profile URL resolution.
    Leads with no profile_urn have a 404/private profile and won't resolve.
    """
    c = _get_campaign_or_404(campaign_id, current_user.id, db)
    updated = (
        db.query(LinkedInConnectionRequest)
        .filter(
            LinkedInConnectionRequest.campaign_id == campaign_id,
            LinkedInConnectionRequest.status == "error",
            LinkedInConnectionRequest.profile_urn.isnot(None),
        )
        .update({"status": "pending", "error": None, "updated_at": datetime.utcnow()})
    )
    db.commit()
    return {"ok": True, "reset": updated}


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
    if c.status not in ("paused", "auth_failed"):
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

@router.post("/campaigns/{campaign_id}/check-auth")
async def check_campaign_auth(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Check whether a LinkedIn token exists and can be decrypted.

    Does NOT make a live Voyager call — LinkedIn blocks datacenter IPs so any
    such call would return a false failure. Auth failure is detected by the
    daemon when it actually tries to send. This endpoint only gates the
    no-token / decrypt-error cases.
    """
    c = _get_campaign_or_404(campaign_id, current_user.id, db)
    token_row = db.query(LinkedInToken).filter(LinkedInToken.user_id == current_user.id).first()
    if not token_row:
        if c.status == "running":
            c.status = "auth_failed"
            c.updated_at = datetime.utcnow()
            db.commit()
        return {"auth_ok": False, "reason": "no_token"}

    try:
        decrypt(token_row.li_at_enc, token_row.nonce)
        decrypt(token_row.jsessionid_enc, token_row.nonce)
    except Exception:
        if c.status == "running":
            c.status = "auth_failed"
            c.updated_at = datetime.utcnow()
            db.commit()
        return {"auth_ok": False, "reason": "decrypt_failed"}

    return {"auth_ok": True}


def _get_campaign_or_404(campaign_id: int, user_id: str, db: Session) -> LinkedInCampaign:
    c = (
        db.query(LinkedInCampaign)
        .filter(LinkedInCampaign.id == campaign_id, LinkedInCampaign.user_id == user_id)
        .first()
    )
    if not c:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return c
