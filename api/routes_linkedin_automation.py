"""LinkedIn automation routes — email+password login, campaign CRUD, launch/pause, stats."""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
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
from services.linkedin_outreach.crypto import decrypt, decrypt_second, encrypt_pair
from services.linkedin_outreach.login import linkedin_check_phone_tap, linkedin_login_start, linkedin_verify_pin
from services.linkedin_outreach.message_gen import generate_connection_message

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/linkedin/automation", tags=["linkedin-automation"])


async def _geo_from_request(request: Request) -> tuple[str | None, str | None]:
    """Geolocate the customer's real IP (behind the ingress) → (country, city),
    and set the proxy-country context so this very login routes through the
    customer's own country. Best-effort; never raises."""
    try:
        from services.linkedin_outreach.geo import client_ip_from_headers, geolocate
        from services.linkedin_outreach.proxy_ctx import proxy_country_var
        ip = client_ip_from_headers(
            request.headers.get("x-forwarded-for"),
            request.headers.get("x-real-ip"),
            request.client.host if request.client else None,
        )
        country, city = await geolocate(ip)
        if country:
            proxy_country_var.set(country)
            logger.info("LinkedIn connect: geolocated client %s -> %s/%s", ip, country, city)
        return country, city
    except Exception as e:
        logger.warning("LinkedIn connect: geolocation failed: %s", e)
        return None, None


# ── Auth: email+password login ─────────────────────────────────────────────────

class LinkedInLoginRequest(BaseModel):
    email: str
    password: str


class LinkedInVerifyPinRequest(BaseModel):
    session_key: str
    pin: str


class LinkedInCheckPhoneTapRequest(BaseModel):
    session_key: str


def _store_token(
    db, user_id: str, li_at: str, jsessionid: str, display_name: str | None,
    connection_mode: str = "proxy",
    cookies_blob: str | None = None,
    proxy_country: str | None = None,
    proxy_city: str | None = None,
):
    import secrets as _secrets
    from services.linkedin_outreach.crypto import encrypt as encrypt_single
    li_at_enc, jsessionid_enc, nonce = encrypt_pair(li_at, jsessionid)
    # Evomi caps sticky-session tokens at ~12 chars before returning 400 on
    # the proxy CONNECT. token_hex(6) = 12 hex chars, well within the limit
    # and still 48 bits of entropy (plenty of uniqueness per user).
    proxy_session_id = _secrets.token_hex(6)

    cookies_blob_enc = None
    cookies_blob_nonce = None
    if cookies_blob:
        cookies_blob_enc, cookies_blob_nonce = encrypt_single(cookies_blob)

    existing = db.query(LinkedInToken).filter(LinkedInToken.user_id == user_id).first()
    if existing:
        existing.li_at_enc = li_at_enc
        existing.jsessionid_enc = jsessionid_enc
        existing.nonce = nonce
        existing.linkedin_name = display_name
        existing.proxy_session_id = proxy_session_id
        existing.connection_mode = connection_mode
        existing.cookies_blob_enc = cookies_blob_enc
        existing.cookies_blob_nonce = cookies_blob_nonce
        # Only overwrite the geolocated country when we actually have one — a
        # later re-login without an IP shouldn't wipe a good value.
        if proxy_country:
            existing.proxy_country = proxy_country
            existing.proxy_city = proxy_city
        existing.updated_at = datetime.utcnow()
    else:
        db.add(LinkedInToken(
            user_id=user_id,
            li_at_enc=li_at_enc,
            jsessionid_enc=jsessionid_enc,
            nonce=nonce,
            linkedin_name=display_name,
            proxy_session_id=proxy_session_id,
            connection_mode=connection_mode,
            cookies_blob_enc=cookies_blob_enc,
            cookies_blob_nonce=cookies_blob_nonce,
            proxy_country=proxy_country,
            proxy_city=proxy_city,
        ))
    db.commit()

    # Fresh credentials → clear any stale auth-failure counts so the next
    # campaign tick starts from zero, not mid-threshold.
    from services.linkedin_outreach.automation_service import reset_auth_fail_count
    from database.models import LinkedInCampaign
    campaigns = db.query(LinkedInCampaign).filter(LinkedInCampaign.user_id == user_id).all()
    for camp in campaigns:
        reset_auth_fail_count(camp.id)

    return display_name


@router.post("/login")
async def login_with_credentials(
    body: LinkedInLoginRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Step 1 — attempt login. Returns ok=True on success, or challenge_required=True + session_key if PIN needed."""
    if not body.email or not body.password:
        raise HTTPException(status_code=400, detail="email and password are required")

    # Geolocate the customer's real IP and pin the proxy exit to their country —
    # both for THIS login (sets the context the proxy builders read) and stored
    # on the token so all future sends egress from the same country.
    proxy_country, proxy_city = await _geo_from_request(request)

    try:
        from core.config import settings as _s
        proxy_url = (_s.LINKEDIN_PROXY_URL or "").strip() or None
        li_at, jsessionid, display_name, session_key, cookies_blob = await linkedin_login_start(
            body.email, body.password, proxy_url=proxy_url
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if li_at is None:
        from services.linkedin_outreach.login import _pending
        challenge_type = _pending.get(session_key, {}).get("challenge_type", "pin")
        # Stash the geo on the pending session so verify-pin/check-phone-tap can persist it.
        if session_key and session_key in _pending:
            _pending[session_key]["proxy_country"] = proxy_country
            _pending[session_key]["proxy_city"] = proxy_city
        return {"ok": False, "challenge_required": True, "session_key": session_key, "challenge_type": challenge_type}

    _store_token(db, current_user.id, li_at, jsessionid, display_name, cookies_blob=cookies_blob,
                 proxy_country=proxy_country, proxy_city=proxy_city)
    return {"ok": True, "linkedin_name": display_name, "proxy_country": proxy_country}


@router.post("/login/verify-pin")
async def verify_pin(
    body: LinkedInVerifyPinRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Step 2 — submit the PIN LinkedIn emailed. Completes login and stores the session."""
    if not body.session_key or not body.pin:
        raise HTTPException(status_code=400, detail="session_key and pin are required")

    from services.linkedin_outreach.login import _pending
    _pc = _pending.get(body.session_key, {}).get("proxy_country")
    _pcity = _pending.get(body.session_key, {}).get("proxy_city")
    try:
        li_at, jsessionid, display_name = await linkedin_verify_pin(body.session_key, body.pin)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    _store_token(db, current_user.id, li_at, jsessionid, display_name,
                 proxy_country=_pc, proxy_city=_pcity)
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
    from services.linkedin_outreach.login import _pending
    _pc = _pending.get(body.session_key, {}).get("proxy_country")
    _pcity = _pending.get(body.session_key, {}).get("proxy_city")
    try:
        result = await linkedin_check_phone_tap(body.session_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result is None:
        return {"ok": False, "still_waiting": True}
    li_at, jsessionid, display_name = result
    _store_token(db, current_user.id, li_at, jsessionid, display_name,
                 proxy_country=_pc, proxy_city=_pcity)
    return {"ok": True, "linkedin_name": display_name}


class LinkedInExtensionCookie(BaseModel):
    name: str
    value: str
    domain: str | None = None
    path: str | None = None
    secure: bool | None = None
    httpOnly: bool | None = None
    sameSite: str | None = None


class LinkedInCookieLoginRequest(BaseModel):
    li_at: str
    jsessionid: str
    is_extension: bool = False  # True when called from the Studojo browser extension
    cookies: list[LinkedInExtensionCookie] | None = None  # Full LinkedIn cookie jar (extension only)


@router.post("/login/cookies")
async def login_with_cookies(
    body: LinkedInCookieLoginRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Store LinkedIn session cookies directly (bypasses email+password challenge).

    If is_extension=True the token is flagged 'extension' connection_mode — the daemon
    skips it (server-side proxy sends fail with redirect loops because the cookies are
    bound to the user's home IP). The extension polls /extension/next-task and runs
    sends from inside the user's browser.
    """
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

    # When the extension sends the full cookie jar, we can use server-side proxy sending.
    # Without the full jar (paste-cookies tab or older extension), extension-login cookies
    # would fail through the proxy — fall back to extension-runs-the-sends mode.
    import json as _json
    cookies_blob = None
    if body.cookies:
        cookies_blob = _json.dumps([c.dict() for c in body.cookies])
        # Full cookie jar present: proxy mode works
        connection_mode = "proxy"
    else:
        connection_mode = "extension" if body.is_extension else "proxy"

    # Geolocate the customer's IP so proxy-mode sends egress from their country.
    proxy_country, proxy_city = await _geo_from_request(request)
    _store_token(
        db, current_user.id, body.li_at, body.jsessionid.strip('"'), display_name,
        connection_mode=connection_mode,
        cookies_blob=cookies_blob,
        proxy_country=proxy_country, proxy_city=proxy_city,
    )
    return {
        "ok": True,
        "linkedin_name": display_name,
        "connection_mode": connection_mode,
        "cookies_count": len(body.cookies) if body.cookies else 0,
    }


# ── Extension-driven send pipeline ─────────────────────────────────────────────
# For users who connected via the browser extension, cookies are bound to their
# home IP — server-side sends via our proxy fail with redirect loops. Instead, the
# extension polls these endpoints from the user's browser (their IP, their cookies)
# and runs the actual LinkedIn connect click via background-tab automation.

@router.get("/extension/next-task")
async def extension_next_task(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the next pending lead to send for this user's active campaigns.

    Only returns work if the user's token is connection_mode='extension'. Respects
    each campaign's daily_limit and the IST sending window (9am–6pm IST).
    """
    from services.linkedin_outreach.automation_service import _is_ist_sending_window

    token = db.query(LinkedInToken).filter(LinkedInToken.user_id == current_user.id).first()
    if not token or token.connection_mode != "extension":
        return {"task": None, "reason": "not_extension_mode"}

    if not _is_ist_sending_window():
        return {"task": None, "reason": "outside_sending_window"}

    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    camps = (
        db.query(LinkedInCampaign)
        .filter(LinkedInCampaign.user_id == current_user.id, LinkedInCampaign.status == "running")
        .all()
    )
    for c in camps:
        sent_today = (
            db.query(LinkedInConnectionRequest)
            .filter(
                LinkedInConnectionRequest.campaign_id == c.id,
                LinkedInConnectionRequest.sent_at >= today_start,
                LinkedInConnectionRequest.status.in_(["sent", "accepted", "followup_sent", "replied"]),
            )
            .count()
        )
        if sent_today >= (c.daily_limit or 0):
            continue
        req = (
            db.query(LinkedInConnectionRequest)
            .filter(
                LinkedInConnectionRequest.campaign_id == c.id,
                LinkedInConnectionRequest.status == "pending",
            )
            .first()
        )
        if req:
            # Mark in_progress so a second poll (or another tab) doesn't pick it up.
            req.status = "in_progress"
            req.updated_at = datetime.utcnow()
            db.commit()
            return {
                "task": {
                    "task_id": req.id,
                    "campaign_id": c.id,
                    "profile_url": req.profile_url,
                    "note": (c.connection_note or "")[:280],
                    "lead_name": req.name or "",
                }
            }
    return {"task": None, "reason": "no_pending"}


class ExtensionTaskResultRequest(BaseModel):
    task_id: int
    success: bool
    error: str | None = None


@router.post("/extension/task-result")
async def extension_task_result(
    body: ExtensionTaskResultRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Record the outcome of an extension-executed send."""
    req = db.query(LinkedInConnectionRequest).filter(LinkedInConnectionRequest.id == body.task_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="task not found")
    camp = db.query(LinkedInCampaign).filter(LinkedInCampaign.id == req.campaign_id).first()
    if not camp or camp.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="not your task")

    if body.success:
        req.status = "sent"
        req.sent_at = datetime.utcnow()
        camp.total_sent = (camp.total_sent or 0) + 1
        camp.updated_at = datetime.utcnow()
    else:
        req.status = "error"
        req.error = (body.error or "Extension send failed")[:500]
    req.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


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
    daily_limit: int = Field(default=10, ge=1, le=20)


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
    weekly_invite_limit: int
    send_with_note: bool
    like_post_before_connect: bool
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
        weekly_invite_limit=c.weekly_invite_limit or 95,
        send_with_note=bool(c.send_with_note),
        like_post_before_connect=bool(c.like_post_before_connect),
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


class CreateCampaignFromProfileRequest(BaseModel):
    candidate_id: int
    name: Optional[str] = None              # if omitted, derived from candidate's target role
    connection_note: Optional[str] = None   # if omitted, AI generates per-lead notes at lead-fetch time
    followup_message: Optional[str] = None
    daily_limit: int = Field(default=5, ge=1, le=20)
    override_target_role: Optional[str] = None       # if student wants to override quiz output
    override_target_locations: Optional[list[str]] = None


@router.post("/campaigns/from-profile", response_model=CampaignResponse)
async def create_campaign_from_profile(
    body: CreateCampaignFromProfileRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create an LKOT campaign whose targeting is auto-derived from a Candidate's
    resume_profile + quiz output (target_roles, target_industries, dream_companies,
    location, etc.). Called after the student finishes the outreach quiz.
    """
    from database.models import Candidate

    cand = db.query(Candidate).filter(
        Candidate.id == body.candidate_id,
        Candidate.user_id == current_user.id,
    ).first()
    if not cand:
        raise HTTPException(status_code=404, detail="Candidate profile not found")

    profile = cand.resume_profile if isinstance(cand.resume_profile, dict) else {}
    target_roles = cand.target_roles if isinstance(cand.target_roles, list) else []
    target_industries = cand.target_industries if isinstance(cand.target_industries, list) else []
    dream_companies = cand.dream_companies if isinstance(cand.dream_companies, list) else []

    # likely_roles is the resume_profile's own role guesses (fallback if quiz roles absent)
    likely_roles = profile.get("likely_roles") if isinstance(profile.get("likely_roles"), list) else []

    # Target role — required field on the campaign schema.
    target_role = (body.override_target_role or "").strip()
    if not target_role and target_roles:
        first = target_roles[0]
        target_role = first if isinstance(first, str) else (first.get("role") or first.get("title") or "")
    if not target_role and likely_roles:
        target_role = str(likely_roles[0])
    if not target_role:
        raise HTTPException(
            status_code=400,
            detail="Could not derive a target role from the profile. Complete the quiz first or pass override_target_role.",
        )

    # Locations — resume_profile.geography is {city, country, country_code}
    locations: list[str] = []
    if body.override_target_locations:
        locations = body.override_target_locations
    else:
        geo = profile.get("geography") if isinstance(profile.get("geography"), dict) else {}
        for key in ("city", "country"):
            v = (geo.get(key) or "").strip()
            if v and v not in locations:
                locations.append(v)

    # Industries — target_industries is a flat list of strings
    industries: list[str] = []
    for ind in target_industries:
        if isinstance(ind, str):
            industries.append(ind)
        elif isinstance(ind, dict):
            v = ind.get("industry") or ind.get("name")
            if v:
                industries.append(str(v))

    # Keyword bias — dream companies + the candidate's top skills
    keyword_parts: list[str] = []
    if dream_companies:
        keyword_parts.extend([str(c.get("name") if isinstance(c, dict) else c) for c in dream_companies if c])
    skills = profile.get("top_skills") or profile.get("skills") or []
    if isinstance(skills, list) and skills:
        keyword_parts.extend([str(s) for s in skills[:5]])
    target_keywords = " ".join(keyword_parts).strip() or None

    name = (body.name or f"{target_role} outreach").strip()

    campaign = LinkedInCampaign(
        user_id=current_user.id,
        candidate_id=cand.id,
        name=name,
        target_role=target_role,
        target_industries=industries,
        target_locations=locations,
        target_company_sizes=[],
        target_keywords=target_keywords,
        connection_note=body.connection_note,
        followup_message=body.followup_message,
        daily_limit=body.daily_limit,
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    logger.info(
        "Campaign %d created from candidate %d — role=%r industries=%s locations=%s",
        campaign.id, cand.id, target_role, industries, locations,
    )
    return _campaign_to_response(campaign)


class CreateCampaignFromOrderRequest(BaseModel):
    order_id: int
    # ~12/day clears a 200-invite plan in 17 days (≈ 2.5 weeks) while staying
    # comfortably under the weekly 92-97 cap. Bounded 1-25 for safety.
    daily_limit: int = Field(default=12, ge=1, le=25)


@router.post("/campaigns/from-order", response_model=CampaignResponse)
async def create_campaign_from_order(
    body: CreateCampaignFromOrderRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a LinkedIn campaign from an OutreachOrder, using Apollo leads that have linkedin_url.

    Pulls leads from the candidate's existing Lead records (no new Apollo calls).
    Generates per-lead connection notes in the background using message_gen.
    Updates the OutreachOrder with the new linkedin_campaign_id.
    """
    from database.models import Candidate, Lead, LeadScore, OutreachOrder

    order = db.query(OutreachOrder).filter_by(
        id=body.order_id, user_id=current_user.id,
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # Token must exist before any re-login path — the caller always hits
    # /login/cookies first, which creates/refreshes the token.
    token_row = db.query(LinkedInToken).filter(LinkedInToken.user_id == current_user.id).first()
    if not token_row:
        raise HTTPException(status_code=400, detail="LinkedIn not connected. Connect first via the LinkedIn tab.")

    # Re-login path A: order already linked to a campaign (normal re-auth).
    # Runs before candidate/credits checks — re-login never needs to create
    # anything new, so no candidate or credits are required.
    if order.linkedin_campaign_id:
        existing = db.query(LinkedInCampaign).filter_by(
            id=order.linkedin_campaign_id, user_id=current_user.id,
        ).first()
        if existing:
            if existing.status in ("auth_failed", "paused"):
                existing.status = "running"
            order.linkedin_connected_at = datetime.utcnow()
            db.commit()
            db.refresh(existing)
            return _campaign_to_response(existing)

    # Re-login path B: user has a campaign but it's not linked to this order.
    # Covers LKOT-migrated users (campaign created before OutreachOrder integration),
    # users whose store drifted to a different order, and any other unlinked state.
    latest_campaign = (
        db.query(LinkedInCampaign)
        .filter_by(user_id=current_user.id)
        .order_by(LinkedInCampaign.id.desc())
        .first()
    )
    if latest_campaign:
        order.linkedin_campaign_id = latest_campaign.id
        order.linkedin_connected_at = datetime.utcnow()
        if latest_campaign.status in ("auth_failed", "paused"):
            latest_campaign.status = "running"
        db.commit()
        db.refresh(latest_campaign)
        return _campaign_to_response(latest_campaign)

    # ── New campaign path ────────────────────────────────────────────────────
    # Only reached when the user has no existing campaign at all.
    # Now we need a candidate and credits.

    candidate_id = order.candidate_id
    if not candidate_id:
        raise HTTPException(status_code=400, detail="Order has no candidate profile linked")

    # Credits check. Fall back to plan_type for orders whose
    # linkedin_credits_reserved column was never backfilled.
    linkedin_limit = getattr(order, "linkedin_credits_reserved", 0) or 0
    if linkedin_limit == 0:
        plan_type = getattr(order, "plan_type", "email") or "email"
        if plan_type in ("linkedin", "both"):
            linkedin_limit = 200
        else:
            raise HTTPException(status_code=400, detail="No LinkedIn credits reserved for this order")

    cand = db.query(Candidate).filter(Candidate.id == candidate_id, Candidate.user_id == current_user.id).first()
    if not cand:
        raise HTTPException(status_code=404, detail="Candidate profile not found")

    # Pull leads ordered best-score first.
    # profile_url may be NULL (Apollo search rarely returns linkedin_url) —
    # the automation daemon resolves it lazily via Voyager just before sending.
    leads_query = (
        db.query(Lead)
        .outerjoin(LeadScore, LeadScore.lead_id == Lead.id)
        .filter(Lead.candidate_id == candidate_id)
        .order_by(LeadScore.overall_score.desc().nullslast())
        .limit(linkedin_limit)
        .all()
    )

    if not leads_query:
        raise HTTPException(
            status_code=400,
            detail="No leads found. Complete the lead discovery step first.",
        )

    # Derive campaign targeting from candidate profile (same logic as from-profile)
    profile = cand.resume_profile if isinstance(cand.resume_profile, dict) else {}
    target_roles = cand.target_roles if isinstance(cand.target_roles, list) else []
    target_industries = cand.target_industries if isinstance(cand.target_industries, list) else []
    likely_roles = profile.get("likely_roles") if isinstance(profile.get("likely_roles"), list) else []

    target_role = ""
    if target_roles:
        first = target_roles[0]
        target_role = first if isinstance(first, str) else (first.get("role") or first.get("title") or "")
    if not target_role and likely_roles:
        target_role = str(likely_roles[0])
    if not target_role:
        target_role = "professional"

    geo = profile.get("geography") if isinstance(profile.get("geography"), dict) else {}
    locations: list[str] = []
    for key in ("city", "country"):
        v = (geo.get(key) or "").strip()
        if v and v not in locations:
            locations.append(v)

    industries: list[str] = []
    for ind in target_industries:
        if isinstance(ind, str):
            industries.append(ind)
        elif isinstance(ind, dict):
            v = ind.get("industry") or ind.get("name")
            if v:
                industries.append(str(v))

    dream_companies = cand.dream_companies if isinstance(cand.dream_companies, list) else []
    keyword_parts: list[str] = []
    if dream_companies:
        keyword_parts.extend([str(c.get("name") if isinstance(c, dict) else c) for c in dream_companies if c])
    skills = profile.get("top_skills") or profile.get("skills") or []
    if isinstance(skills, list) and skills:
        keyword_parts.extend([str(s) for s in skills[:5]])
    target_keywords = " ".join(keyword_parts).strip() or None

    import random as _random
    # Randomised weekly cap so concurrent campaigns don't all hit the same
    # round number — looks more human and stays safely under LinkedIn's
    # ~100/week soft limit. With ~12/day + 92-97/week cap, a user clears
    # the 200-invite plan in ~16-18 days.
    weekly_cap = _random.randint(92, 97)

    campaign = LinkedInCampaign(
        user_id=current_user.id,
        candidate_id=cand.id,
        name=f"{target_role} outreach",
        status="running",
        target_role=target_role,
        target_industries=industries,
        target_locations=locations,
        target_company_sizes=[],
        target_keywords=target_keywords,
        daily_limit=body.daily_limit,
        weekly_invite_limit=weekly_cap,
        total_leads=len(leads_query),
        launched_at=datetime.utcnow(),
    )
    db.add(campaign)
    db.flush()  # get campaign.id before creating requests

    # Create connection requests from Apollo leads
    request_ids: list[int] = []
    for lead in leads_query:
        req = LinkedInConnectionRequest(
            campaign_id=campaign.id,
            user_id=current_user.id,
            name=lead.name or "",
            headline=lead.title or "",
            company=lead.company or "",
            location=lead.location or "",
            profile_url=lead.linkedin_url,
            status="pending",
        )
        db.add(req)
        db.flush()
        request_ids.append(req.id)

    # Link campaign back to the order
    order.linkedin_campaign_id = campaign.id
    order.linkedin_connected_at = datetime.utcnow()

    db.commit()
    db.refresh(campaign)

    # Generate per-lead connection notes in the background
    def _gen_notes(camp_id: int, req_ids: list[int]) -> None:
        from database.session import SessionLocal
        from services.linkedin_outreach.message_gen import generate_connection_message
        bg_db = SessionLocal()
        try:
            for req_id in req_ids:
                req = bg_db.query(LinkedInConnectionRequest).filter_by(id=req_id).first()
                if not req:
                    continue
                camp = bg_db.query(LinkedInCampaign).filter_by(id=camp_id).first()
                if not camp:
                    continue
                try:
                    msg = generate_connection_message(
                        target_role=camp.target_role,
                        lead_name=req.name or "",
                        lead_headline=req.headline or "",
                        lead_company=req.company or "",
                        candidate_profile={},
                    )
                    req.connection_note = msg
                    req.updated_at = datetime.utcnow()
                    bg_db.commit()
                except Exception:
                    pass
        finally:
            bg_db.close()

    import threading
    threading.Thread(
        target=_gen_notes,
        args=(campaign.id, request_ids),
        daemon=True,
    ).start()

    logger.info(
        "Campaign %d created from order %d — %d leads, role=%r",
        campaign.id, body.order_id, len(leads_query), target_role,
    )
    return _campaign_to_response(campaign)


@router.get("/my-candidate")
async def my_candidate(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the user's most recent candidate profile (or null).

    LKOT Profile step calls this to decide whether to show 'continue with your
    profile' or send them through the outreach upload + quiz flow first.
    """
    from database.models import Candidate

    cand = (
        db.query(Candidate)
        .filter(Candidate.user_id == current_user.id)
        .order_by(Candidate.created_at.desc())
        .first()
    )
    if not cand:
        return {"candidate": None}

    profile = cand.resume_profile if isinstance(cand.resume_profile, dict) else {}
    target_roles = cand.target_roles if isinstance(cand.target_roles, list) else []
    target_industries = cand.target_industries if isinstance(cand.target_industries, list) else []
    likely_roles = profile.get("likely_roles") if isinstance(profile.get("likely_roles"), list) else []
    # Quiz is "complete" once the student has confirmed target roles/industries.
    quiz_complete = bool(target_roles or target_industries)

    primary_role = ""
    if target_roles:
        first = target_roles[0]
        primary_role = first if isinstance(first, str) else (first.get("role") or first.get("title") or "")
    if not primary_role and likely_roles:
        primary_role = str(likely_roles[0])

    geo = profile.get("geography") if isinstance(profile.get("geography"), dict) else {}
    location = " ".join(str(geo.get(k, "")) for k in ("city", "country") if geo.get(k)).strip() or None

    return {
        "candidate": {
            "id": cand.id,
            "primary_role": primary_role,
            "target_roles": target_roles,
            "target_industries": target_industries,
            "dream_companies": cand.dream_companies or [],
            "skills": profile.get("top_skills") or profile.get("skills") or [],
            "location": location,
            "quiz_complete": quiz_complete,
            "created_at": cand.created_at.isoformat() if cand.created_at else None,
        }
    }


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


class UpdateCampaignSettingsRequest(BaseModel):
    send_with_note: Optional[bool] = None
    like_post_before_connect: Optional[bool] = None


@router.patch("/campaigns/{campaign_id}/settings", response_model=CampaignResponse)
async def update_campaign_settings(
    campaign_id: int,
    body: UpdateCampaignSettingsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Toggle operationally-safe settings without pausing the campaign.

    These toggles only affect future sends; in-flight invites and the daemon's
    pacing are unaffected, so there's no reason to force a pause/resume cycle.
    """
    c = _get_campaign_or_404(campaign_id, current_user.id, db)
    if body.send_with_note is not None:
        c.send_with_note = body.send_with_note
    if body.like_post_before_connect is not None:
        c.like_post_before_connect = body.like_post_before_connect
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
    """Search LinkedIn for leads matching the campaign ICP and save them.

    Lead discovery uses public web search (DDG x-ray) and needs NO LinkedIn
    session — leads appear before the user connects. A connected account is
    only required later, at send-time, to fire connection requests.
    """
    c = _get_campaign_or_404(campaign_id, current_user.id, db)

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
    from services.linkedin_outreach.web_discovery import discover_leads_via_search

    # 1. Read campaign params with a short-lived session. We must NOT hold a DB
    # connection open across the web discovery below — it takes 1-2 min and
    # Postgres closes the idle connection, breaking the later write.
    db = SessionLocal()
    try:
        c = db.query(LinkedInCampaign).filter(LinkedInCampaign.id == campaign_id).first()
        if not c:
            return
        target_role = c.target_role
        target_locations = c.target_locations or []
        target_industries = c.target_industries or []
        target_keywords = c.target_keywords
    finally:
        db.close()

    logger.info(
        "Starting lead search for campaign %d: role=%r locations=%r industries=%r keywords=%r",
        campaign_id, target_role, target_locations, target_industries, target_keywords,
    )

    # 2. Login-free discovery via public web search (DDG x-ray). No LinkedIn
    # session, no Apollo credits, and no DB connection held while it runs.
    people = await discover_leads_via_search(
        target_role=target_role,
        locations=target_locations,
        industries=target_industries,
        keywords=target_keywords,
        limit=30,
    )

    logger.info("Lead search for campaign %d returned %d people", campaign_id, len(people))

    # 3. Write results with a FRESH session.
    db = SessionLocal()
    try:
        c = db.query(LinkedInCampaign).filter(LinkedInCampaign.id == campaign_id).first()
        if not c:
            return

        if not people:
            logger.warning("No leads found for campaign %d — web search returned nothing", campaign_id)
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

        # Build a student summary from the linked candidate (if any) so match_reason
        # references the student's real background, not just the target role.
        student_summary = None
        if c.candidate_id:
            from database.models import Candidate
            cand = db.query(Candidate).filter(Candidate.id == c.candidate_id).first()
            if cand:
                student_summary = _summarise_candidate(cand, c.target_role)

        # Fire-and-forget AI personalisation — does not block lead availability
        import asyncio as _asyncio
        _asyncio.create_task(
            _personalise_leads(req_ids, people, c.target_role, user_name, c.connection_note, student_summary)
        )

    except Exception as e:
        logger.error("Lead search failed for campaign %d: %s", campaign_id, e, exc_info=True)
    finally:
        db.close()


def _summarise_candidate(cand, target_role: str) -> str:
    """One-paragraph factual summary of a Candidate row, used to ground the
    per-lead match_reason prompt in the student's actual background.

    Reads the resume_profile schema produced by resume_intelligence:
    {domain, subdomain, seniority, geography:{city,country}, top_skills,
     likely_roles, archetype_label, strongest_hook, candidate_pitch}.
    """
    profile = cand.resume_profile if isinstance(cand.resume_profile, dict) else {}
    parts: list[str] = []

    archetype = profile.get("archetype_label")
    if archetype:
        parts.append(str(archetype))

    seniority = profile.get("seniority")
    domain = profile.get("domain")
    subdomain = profile.get("subdomain")
    field = " / ".join(str(x).replace("_", " ") for x in (domain, subdomain) if x)
    if seniority or field:
        parts.append(f"{(seniority or '').strip().capitalize()} in {field}".strip())

    skills = profile.get("top_skills") or profile.get("skills") or []
    if isinstance(skills, list) and skills:
        parts.append(f"Skills: {', '.join(str(s) for s in skills[:6])}")

    geo = profile.get("geography") if isinstance(profile.get("geography"), dict) else {}
    loc = " ".join(str(geo.get(k, "")) for k in ("city", "country") if geo.get(k)).strip()
    if loc:
        parts.append(f"Based in {loc}")

    parts.append(f"Looking for: {target_role}")
    return ". ".join(p for p in parts if p)[:600]


async def _personalise_leads(
    req_ids: list[int],
    people: list[dict],
    target_role: str,
    user_name: str,
    template: str | None,
    student_summary: str | None = None,
) -> None:
    """Background task: update saved leads with AI-personalised connection notes AND
    a one-line match reason. Runs after lead save so the dashboard sees leads immediately."""
    import asyncio as _asyncio
    from database.session import SessionLocal
    from services.linkedin_outreach.message_gen import generate_connection_message, generate_match_reason

    db = SessionLocal()
    try:
        # Generate note + match reason concurrently per lead, batched to respect OpenAI rate limits
        async def enrich_one(req_id: int, person: dict) -> tuple[int, str, str]:
            try:
                note_task = generate_connection_message(
                    person_name=person.get("name", ""),
                    person_headline=person.get("headline", ""),
                    person_company=person.get("company", ""),
                    target_role=target_role,
                    student_name=user_name,
                )
                reason_task = generate_match_reason(
                    person_name=person.get("name", ""),
                    person_headline=person.get("headline", ""),
                    person_company=person.get("company", ""),
                    target_role=target_role,
                    student_summary=student_summary,
                )
                note, reason = await _asyncio.gather(note_task, reason_task)
                return req_id, note, reason
            except Exception:
                return req_id, "", ""

        batch_size = 5
        for i in range(0, len(req_ids), batch_size):
            batch = list(zip(req_ids, people))[i : i + batch_size]
            results = await _asyncio.gather(*[enrich_one(rid, p) for rid, p in batch])
            for req_id, note, reason in results:
                req = db.query(LinkedInConnectionRequest).filter(LinkedInConnectionRequest.id == req_id).first()
                if not req or req.status != "pending":
                    continue
                if note:
                    req.connection_note = note
                if reason:
                    req.match_reason = reason
            db.commit()
        logger.info("Personalised %d leads (note + match_reason) in background", len(req_ids))
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
        jsessionid = decrypt_second(token_row.jsessionid_enc, token_row.nonce)
    except Exception:
        c.status = "auth_failed"
        c.updated_at = datetime.utcnow()
        db.commit()
        raise HTTPException(status_code=400, detail="Token decryption failed — reconnect LinkedIn")

    # Try up to 5 pending requests — skip any that can't be resolved
    candidates = (
        db.query(LinkedInConnectionRequest)
        .filter(
            LinkedInConnectionRequest.campaign_id == campaign_id,
            LinkedInConnectionRequest.status == "pending",
        )
        .limit(5)
        .all()
    )
    if not candidates:
        raise HTTPException(status_code=404, detail="No pending leads to send")

    from services.linkedin_outreach.automation_service import _decrypt_cookies_blob
    from services.linkedin_outreach.voyager import resolve_linkedin_url
    from core.config import settings as _settings
    cookies_blob = _decrypt_cookies_blob(token_row)
    proxy_url = (_settings.LINKEDIN_PROXY_URL or "").strip() or None

    req = None
    for candidate in candidates:
        if not candidate.profile_url:
            first_name = (candidate.name or "").split()[0] if candidate.name else ""
            try:
                resolved = await resolve_linkedin_url(
                    first_name, candidate.company or "", candidate.headline or "",
                    li_at, jsessionid, proxy_url=proxy_url,
                )
            except Exception:
                resolved = None
            if resolved:
                candidate.profile_url = resolved
                db.commit()
                req = candidate
                break
            else:
                candidate.status = "error"
                candidate.error = "Could not resolve LinkedIn profile"
                candidate.updated_at = datetime.utcnow()
                db.commit()
        else:
            req = candidate
            break

    if not req:
        raise HTTPException(status_code=404, detail="Could not resolve any pending leads — try again later")

    # Resolve URN (best-effort — Playwright fallback works without it).
    # Manual sends do NOT flip the campaign to auth_failed on a single
    # LinkedInAuthError. LinkedIn returns HTTP 999 / login redirects on
    # transient rate-limits + IP blocks too, and killing the campaign
    # permanently on one click is too aggressive. The daemon handles real
    # auth death across many ticks; one-off manual sends just report back.
    if not req.profile_urn:
        try:
            urn = await resolve_profile_urn(li_at, jsessionid, req.profile_url, token_row.proxy_session_id, cookies_blob)
        except LinkedInAuthError as e:
            raise HTTPException(
                status_code=502,
                detail=f"LinkedIn rejected this send: {e}. If this keeps happening, reconnect LinkedIn.",
            )
        if urn:
            req.profile_urn = urn

    try:
        ok, send_error = await send_connection_request(
            li_at, jsessionid,
            req.profile_urn or "",
            req.connection_note or "",
            token_row.proxy_session_id,
            profile_url=req.profile_url,
            cookies_blob=cookies_blob,
        )
    except LinkedInAuthError as e:
        raise HTTPException(
            status_code=502,
            detail=f"LinkedIn rejected this send: {e}. If this keeps happening, reconnect LinkedIn.",
        )

    if ok:
        req.status = "sent"
        req.sent_at = datetime.utcnow()
        c.total_sent += 1
        result = "sent"
        send_error = ""
    else:
        req.status = "error"
        req.error = send_error or "Send failed"
        result = "error"

    req.updated_at = datetime.utcnow()
    c.updated_at = datetime.utcnow()
    db.commit()

    return {"ok": ok, "result": result, "lead_name": req.name, "profile_url": req.profile_url, "error_detail": send_error}


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
    from services.linkedin_outreach.automation_service import reset_auth_fail_count
    reset_auth_fail_count(c.id)
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
    profile_url: Optional[str]
    connection_note: Optional[str]
    followup_message: Optional[str]
    match_reason: Optional[str]
    status: str
    sent_at: Optional[str]
    accepted_at: Optional[str]
    followup_sent_at: Optional[str]
    reply_text: Optional[str]
    reply_sentiment: Optional[str]
    error: Optional[str] = None


class MarkSentBody(BaseModel):
    profile_urn: Optional[str] = None


@router.post("/campaigns/{campaign_id}/leads/{lead_id}/mark-sent")
async def mark_lead_sent(
    campaign_id: int,
    lead_id: int,
    body: MarkSentBody,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark a lead as sent externally (used by the local_sender.py script running from MacBook IP)."""
    c = _get_campaign_or_404(campaign_id, current_user.id, db)
    req = (
        db.query(LinkedInConnectionRequest)
        .filter(
            LinkedInConnectionRequest.id == lead_id,
            LinkedInConnectionRequest.campaign_id == campaign_id,
        )
        .first()
    )
    if not req:
        raise HTTPException(status_code=404, detail="Lead not found")
    if req.status == "sent":
        return {"ok": True, "already_sent": True}
    req.status = "sent"
    req.sent_at = datetime.utcnow()
    if body.profile_urn:
        req.profile_urn = body.profile_urn
    req.updated_at = datetime.utcnow()
    c.total_sent += 1
    c.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


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

    # Show non-pending (sent/accepted/replied/error) first so progress is always visible,
    # then pending by created_at asc so the queue order is preserved.
    from sqlalchemy import case as sa_case
    priority = sa_case(
        (LinkedInConnectionRequest.status == "pending", 1),
        (LinkedInConnectionRequest.status == "error", 2),
        else_=0,
    )
    requests = q.order_by(priority, LinkedInConnectionRequest.created_at.asc()).limit(limit).all()

    return [
        ConnectionRequestResponse(
            id=r.id,
            name=r.name,
            headline=r.headline,
            company=r.company,
            profile_url=r.profile_url,
            connection_note=r.connection_note,
            followup_message=r.followup_message,
            match_reason=r.match_reason,
            status=r.status,
            sent_at=r.sent_at.isoformat() if r.sent_at else None,
            accepted_at=r.accepted_at.isoformat() if r.accepted_at else None,
            followup_sent_at=r.followup_sent_at.isoformat() if r.followup_sent_at else None,
            reply_text=r.reply_text,
            reply_sentiment=r.reply_sentiment,
            error=r.error,
        )
        for r in requests
    ]


# ── Inbox ──────────────────────────────────────────────────────────────────────
# Lightweight inbox built on the data the daemon already collects (reply_text /
# reply_received_at / accepted_at on each connection request). For deeper thread
# history the user can click through to LinkedIn; replies sent from here go
# through the same Voyager + Playwright-fallback pipeline as automated
# follow-ups, so they look identical from LinkedIn's perspective.

class InboxConversation(BaseModel):
    request_id: int
    name: str
    headline: Optional[str]
    company: Optional[str]
    profile_url: Optional[str]
    profile_image_url: Optional[str]
    status: str  # accepted | followup_sent | replied
    accepted_at: Optional[str]
    followup_sent_at: Optional[str]
    followup_message: Optional[str]
    reply_text: Optional[str]
    reply_sentiment: Optional[str]
    reply_received_at: Optional[str]
    last_activity_at: Optional[str]  # max of the above timestamps — for sort


class InboxMessage(BaseModel):
    direction: str   # "out" | "in"
    text: str
    sent_at: Optional[str]


class InboxThreadResponse(BaseModel):
    request_id: int
    name: str
    profile_url: Optional[str]
    messages: list[InboxMessage]


class InboxReplyRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


def _conversation_from_request(r: LinkedInConnectionRequest) -> InboxConversation:
    last = max(
        [t for t in (r.reply_received_at, r.followup_sent_at, r.accepted_at) if t],
        default=None,
    )
    return InboxConversation(
        request_id=r.id,
        name=r.name,
        headline=r.headline,
        company=r.company,
        profile_url=r.profile_url,
        profile_image_url=r.profile_image_url,
        status=r.status,
        accepted_at=r.accepted_at.isoformat() if r.accepted_at else None,
        followup_sent_at=r.followup_sent_at.isoformat() if r.followup_sent_at else None,
        followup_message=r.followup_message,
        reply_text=r.reply_text,
        reply_sentiment=r.reply_sentiment,
        reply_received_at=r.reply_received_at.isoformat() if r.reply_received_at else None,
        last_activity_at=last.isoformat() if last else None,
    )


@router.get("/campaigns/{campaign_id}/inbox", response_model=list[InboxConversation])
async def inbox_list(
    campaign_id: int,
    only_replies: bool = False,
    limit: int = 200,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List inbox conversations — accepted, followed-up, or replied.

    Default returns everyone who's accepted (those are the people the user can
    actually message). Set only_replies=true to filter to people who've replied.
    """
    _get_campaign_or_404(campaign_id, current_user.id, db)

    statuses = ["replied"] if only_replies else ["accepted", "followup_sent", "replied"]

    # Sort: replies first (newest), then followup_sent, then accepted (newest accept).
    # Use a CASE for status priority + the relevant timestamp for in-bucket ordering.
    from sqlalchemy import case as sa_case, desc, func as sa_func
    status_priority = sa_case(
        (LinkedInConnectionRequest.status == "replied", 0),
        (LinkedInConnectionRequest.status == "followup_sent", 1),
        else_=2,
    )
    activity_ts = sa_func.coalesce(
        LinkedInConnectionRequest.reply_received_at,
        LinkedInConnectionRequest.followup_sent_at,
        LinkedInConnectionRequest.accepted_at,
    )

    rows = (
        db.query(LinkedInConnectionRequest)
        .filter(
            LinkedInConnectionRequest.campaign_id == campaign_id,
            LinkedInConnectionRequest.status.in_(statuses),
        )
        .order_by(status_priority.asc(), desc(activity_ts))
        .limit(limit)
        .all()
    )
    return [_conversation_from_request(r) for r in rows]


@router.get("/campaigns/{campaign_id}/inbox/{request_id}", response_model=InboxThreadResponse)
async def inbox_thread(
    campaign_id: int,
    request_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the message thread we know about for one connection request.

    Sources, in order: the original connection note, the AI follow-up message,
    and the latest captured reply. For full LinkedIn-side history the user
    clicks through to the profile — fetching full conversation history
    through the proxy is unreliable enough that we don't surface it here.
    """
    _get_campaign_or_404(campaign_id, current_user.id, db)
    r = (
        db.query(LinkedInConnectionRequest)
        .filter(
            LinkedInConnectionRequest.id == request_id,
            LinkedInConnectionRequest.campaign_id == campaign_id,
        )
        .first()
    )
    if not r:
        raise HTTPException(status_code=404, detail="Request not found in this campaign")

    msgs: list[InboxMessage] = []
    if r.connection_note:
        msgs.append(InboxMessage(
            direction="out", text=r.connection_note,
            sent_at=r.sent_at.isoformat() if r.sent_at else None,
        ))
    if r.followup_message and r.followup_sent_at:
        msgs.append(InboxMessage(
            direction="out", text=r.followup_message,
            sent_at=r.followup_sent_at.isoformat(),
        ))
    if r.reply_text:
        msgs.append(InboxMessage(
            direction="in", text=r.reply_text,
            sent_at=r.reply_received_at.isoformat() if r.reply_received_at else None,
        ))

    return InboxThreadResponse(
        request_id=r.id, name=r.name, profile_url=r.profile_url, messages=msgs,
    )


@router.post("/campaigns/{campaign_id}/inbox/{request_id}/reply")
async def inbox_reply(
    campaign_id: int,
    request_id: int,
    body: InboxReplyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Send a manual reply on top of the daemon's automated thread.

    Goes through the same Voyager + Playwright fallback used for follow-ups
    so it doesn't tip LinkedIn off as a separate sender. We append the sent
    message to the request's followup_message field (last-write-wins) and
    bump followup_sent_at for inbox ordering.
    """
    from services.linkedin_outreach.automation_service import send_message, LinkedInAuthError
    from services.linkedin_outreach.crypto import decrypt, decrypt_second, decrypt_single

    campaign = _get_campaign_or_404(campaign_id, current_user.id, db)
    r = (
        db.query(LinkedInConnectionRequest)
        .filter(
            LinkedInConnectionRequest.id == request_id,
            LinkedInConnectionRequest.campaign_id == campaign_id,
        )
        .first()
    )
    if not r:
        raise HTTPException(status_code=404, detail="Request not found")
    # Replies only make sense once they've accepted — LinkedIn rejects DMs
    # to non-1st-degree connections anyway.
    if r.status not in ("accepted", "followup_sent", "replied"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot send reply yet — connection status is '{r.status}'",
        )

    token = db.query(LinkedInToken).filter(LinkedInToken.user_id == current_user.id).first()
    if not token:
        raise HTTPException(status_code=400, detail="LinkedIn not connected")

    try:
        li_at = decrypt(token.li_at_enc, token.nonce)
        jsessionid = decrypt_second(token.jsessionid_enc, token.nonce)
    except Exception:
        raise HTTPException(status_code=401, detail="LinkedIn session decrypt failed — reconnect")

    cookies_blob = None
    if getattr(token, "cookies_blob_enc", None) and getattr(token, "cookies_blob_nonce", None):
        try:
            cookies_blob = decrypt_single(token.cookies_blob_enc, token.cookies_blob_nonce)
        except Exception:
            cookies_blob = None

    try:
        ok = await send_message(
            li_at, jsessionid,
            r.profile_urn or "",
            body.text,
            session_id=f"user_{current_user.id}",
            profile_url=r.profile_url,
            cookies_blob=cookies_blob,
        )
    except LinkedInAuthError:
        campaign.status = "auth_failed"
        campaign.updated_at = datetime.utcnow()
        db.commit()
        raise HTTPException(status_code=401, detail="LinkedIn session expired — reconnect first")

    if not ok:
        raise HTTPException(status_code=502, detail="LinkedIn rejected the message. Try again in a minute.")

    # Treat manual reply as the latest activity on this thread.
    r.followup_message = body.text
    r.followup_sent_at = datetime.utcnow()
    if r.status == "accepted":
        r.status = "followup_sent"
    r.updated_at = datetime.utcnow()

    # Campaign-level counter
    campaign.total_followups_sent = (campaign.total_followups_sent or 0) + 1
    campaign.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(r)
    return {"ok": True, "sent_at": r.followup_sent_at.isoformat()}


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
        decrypt_second(token_row.jsessionid_enc, token_row.nonce)
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
