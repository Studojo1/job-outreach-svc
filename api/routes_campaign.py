"""Campaign Routes — Campaign lifecycle, templates, and analytics."""

import asyncio
import logging
import time
from datetime import datetime
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel
from typing import Optional, List

from database.session import get_db
from database.models import User, Campaign
from services.email_campaign.campaign_service import (
    create_campaign,
    transition_campaign,
    get_campaign_metrics,
)
from api.dependencies import get_current_user
from core.analytics import capture

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/campaign", tags=["Campaign"])


class TestEmailRecipient(BaseModel):
    first_name: str
    company: str
    email: str


class SendTestEmailsRequest(BaseModel):
    recipients: List[TestEmailRecipient]

# Pre-built email templates (legacy fallback for non-AI mode)
# When AI styles are selected, the structured pipeline in email_generator_service.py
# handles generation instead.
EMAIL_TEMPLATES = [
    {
        "id": 1,
        "name": "Warm Introduction",
        "subject": "quick intro",
        "body": "Hi {name},\n\nI noticed {company} recently and wanted to reach out. I've been working in the space and your team caught my eye.\n\nWould you mind pointing me in the right direction if there's anyone I should talk to?\n\nAppreciate your time either way.\n\nBest,",
    },
    {
        "id": 2,
        "name": "Skills-Based Pitch",
        "subject": "saw your team at {company}",
        "body": "Hi {name},\n\nI've been building some projects in the same area {company} works in and thought it'd be worth reaching out. I'm looking for a role where I can keep working on similar problems.\n\nIs there someone on the team I should connect with?\n\nThanks for reading.",
    },
    {
        "id": 3,
        "name": "Company Curiosity",
        "subject": "curious about {company}",
        "body": "Hey {name},\n\nCame across {company} while looking at teams in the space and got curious about what you're building. I've been spending time on related work and would love to learn more.\n\nWould you have a few minutes to chat sometime?\n\nCheers,",
    },
    {
        "id": 4,
        "name": "Peer Connect",
        "subject": "quick question about {company}",
        "body": "Hi {name},\n\nI saw your role at {company} and thought we might share some overlapping interests. I've been working on a few things in the same area and figured it was worth saying hi.\n\nWould you be up for a quick chat? No worries if not.\n\nThanks,",
    },
    {
        "id": 5,
        "name": "Direct Outreach",
        "subject": "looking for roles at {company}",
        "body": "Hi {name},\n\nI'm exploring roles in the area {company} works in. I've got some relevant experience and wanted to see if there's anyone on the team I should reach out to.\n\nWould appreciate any direction.\n\nThanks,",
    },
]


class CampaignCreateRequest(BaseModel):
    candidate_id: int
    email_account_id: int
    name: str
    template_id: int = 1
    subject_template: str = ""
    body_template: str = ""
    selected_styles: list[str] = []  # Email styles for AI generation
    user_timezone: str = "Asia/Kolkata"
    lead_limit: Optional[int] = None  # Max leads to include (defaults to all)


class CampaignTransitionRequest(BaseModel):
    target_status: str


def _resolve_effective_candidate(db: Session, user_id: str, candidate_id: int) -> int:
    """Resolve the candidate a campaign op should actually use.

    Re-onboarding (and the Rs.499 launch flow) can leave a user pointed at a
    freshly-created candidate that never finished the quiz (no career_analysis),
    which dead-ended launch with "complete the career quiz" even though the user
    HAD completed it on another record. If the requested candidate is incomplete
    but the user has a more-recent complete candidate that has leads, switch to
    it and rebind the active order so the whole flow follows. Falls back to the
    requested id when there's no better candidate (preserves original behaviour).
    """
    from database.models import Candidate, Lead

    def _complete(c) -> bool:
        return bool(c and c.parsed_json and c.parsed_json.get("career_analysis"))

    requested = db.query(Candidate).filter_by(id=candidate_id, user_id=user_id).first()
    if _complete(requested):
        return candidate_id

    candidates = (
        db.query(Candidate)
        .filter(Candidate.user_id == user_id)
        .order_by(Candidate.created_at.desc())
        .all()
    )
    for c in candidates:
        if c.id == candidate_id or not _complete(c):
            continue
        if db.query(Lead).filter(Lead.candidate_id == c.id).count() == 0:
            continue
        logger.warning(
            "[CAMPAIGN] candidate %s is incomplete for user %s; auto-rebinding to "
            "complete candidate %s (with leads)", candidate_id, user_id, c.id,
        )
        try:
            from services.stage_tracking import get_or_create_active_order
            order = get_or_create_active_order(db, str(user_id))
            if order and order.candidate_id != c.id:
                order.candidate_id = c.id
                db.commit()
        except Exception:
            db.rollback()
            logger.exception("[CAMPAIGN] failed to rebind active order to candidate %s", c.id)
        return c.id

    return candidate_id


def _test_leads(db: Session, candidate_id: int, limit: int = 5):
    """Leads to use for the deliverability test.

    Prefer leads with a verified email, but fall back to ANY leads when none are
    verified yet. The deliverability test sends to the user's OWN inbox (the lead
    is only used to generate sample content), so a verified lead email isn't
    required. Without this fallback, fresh setups whose JIT enrichment hadn't run
    dead-ended with 'No leads with verified emails found'.
    """
    from database.models import Lead
    q = db.query(Lead).filter(Lead.candidate_id == candidate_id)
    verified = (
        q.filter(Lead.email.isnot(None), Lead.email_verified == True)
        .limit(limit).all()
    )
    if verified:
        return verified
    return q.limit(limit).all()


@router.get("/templates")
async def get_templates(current_user: User = Depends(get_current_user)):
    """Return pre-built email templates."""
    return {"templates": EMAIL_TEMPLATES}


@router.get("/validate")
async def validate_campaign_readiness(
    candidate_id: int,
    email_account_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Pre-launch validation: check leads, Gmail, and profile are ready."""
    from database.models import Candidate, Lead, EmailAccount

    # Auto-rebind to a complete candidate if the bound one never finished the quiz.
    candidate_id = _resolve_effective_candidate(db, current_user.id, candidate_id)

    # Check candidate profile exists
    candidate = db.query(Candidate).filter_by(id=candidate_id, user_id=current_user.id).first()
    if not candidate:
        return {"valid": False, "reason": "Candidate profile not found. Complete the quiz first."}

    if not candidate.parsed_json or not candidate.parsed_json.get("career_analysis"):
        return {"valid": False, "reason": "Candidate profile incomplete. Please complete the career quiz."}

    # Check Gmail account
    account = db.query(EmailAccount).filter_by(id=email_account_id).first()
    if not account:
        return {"valid": False, "reason": "Gmail account not connected. Please connect your Gmail first."}

    if not account.access_token:
        return {"valid": False, "reason": "Gmail access token missing. Please reconnect your Gmail."}

    # Check that leads exist (JIT: enrichment happens later, so just check any leads exist)
    lead_count = db.query(Lead).filter(Lead.candidate_id == candidate_id).count()

    if lead_count == 0:
        return {"valid": False, "reason": "No leads found. Run lead discovery first."}

    enriched_count = db.query(Lead).filter(
        Lead.candidate_id == candidate_id,
        Lead.email.isnot(None),
        Lead.email_verified == True,
    ).count()

    return {
        "valid": True,
        "total_leads": lead_count,
        "enriched_leads": enriched_count,
        "email_account": account.email_address,
    }


@router.post("/create")
async def api_create_campaign(
    request: CampaignCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new outreach campaign with AI-generated or template-based emails.

    If selected_styles is provided (non-empty list), generates fully AI-personalized emails.
    Otherwise, uses legacy template substitution.
    """
    try:
        # Auto-rebind to a complete candidate if the bound one never finished the
        # quiz (re-onboarding / Rs.499 dup-candidate case). Keeps the whole create
        # flow on the candidate that actually has a profile + leads.
        request.candidate_id = _resolve_effective_candidate(db, current_user.id, request.candidate_id)

        # Credit check — use SELECT FOR UPDATE to lock the row and prevent race conditions
        # where two simultaneous requests both read the same balance and both pass.
        # This allows multiple campaigns (e.g. 3x200 with 600 credits) but blocks double-clicks.
        from api.routes_payment import deduct_credits
        from database.models import UserCredit
        from sqlalchemy import text
        credits = db.query(UserCredit).filter_by(user_id=current_user.id).with_for_update().first()
        available = (credits.total_credits - credits.used_credits) if credits else 0
        requested = request.lead_limit or 200  # default campaign size
        # Minimum credits to start a campaign — set to the smallest plan (50) so
        # 50-credit plan users can launch. Campaign size is still capped at the
        # user's available balance below.
        MIN_CAMPAIGN_CREDITS = 50
        if available < MIN_CAMPAIGN_CREDITS:
            raise HTTPException(
                status_code=402,
                detail=f"Insufficient credits. You need at least {MIN_CAMPAIGN_CREDITS} credits to start a campaign but only have {available} available.",
            )
        # Cap at available credits — prevents error when setup was done at a higher tier than paid
        required = min(requested, available)
        request.lead_limit = required
        # Reserve credits immediately so concurrent requests see the updated balance
        if credits:
            credits.used_credits += required
            db.flush()  # write to DB within transaction before slow AI generation

        # The style system is retired: there is one email shape, and voice is set by
        # the lead's inferred team-size band. `selected_styles` is now only a legacy
        # flag distinguishing AI generation from template mode; its contents are ignored.
        if not request.selected_styles:
            request.selected_styles = ["ai"]

        # Use AI generation if styles are selected
        if request.selected_styles:
            result = create_campaign(
                db=db,
                user_id=current_user.id,
                name=request.name,
                email_account_id=request.email_account_id,
                candidate_id=request.candidate_id,
                selected_styles=request.selected_styles,
                user_timezone=request.user_timezone,
                lead_limit=request.lead_limit,
            )
        else:
            # Fall back to template mode
            subject = request.subject_template
            body = request.body_template
            if not subject or not body:
                template = next(
                    (t for t in EMAIL_TEMPLATES if t["id"] == request.template_id),
                    EMAIL_TEMPLATES[0],
                )
                subject = subject or template["subject"]
                body = body or template["body"]

            result = create_campaign(
                db=db,
                user_id=current_user.id,
                name=request.name,
                email_account_id=request.email_account_id,
                candidate_id=request.candidate_id,
                subject_template=subject,
                body_template=body,
                user_timezone=request.user_timezone,
            )
        # Funnel: prefer the user's *active* OutreachOrder (created at resume
        # upload) and advance it to campaign_setup. Falls back to creating a
        # new row only if none exists, so we don't fragment a user's history.
        try:
            from datetime import datetime
            from services.stage_tracking import get_or_create_active_order, safe_mark_stage
            order = get_or_create_active_order(db, str(current_user.id), candidate_id=request.candidate_id)
            order.candidate_id = order.candidate_id or request.candidate_id
            order.campaign_id = result["campaign_id"]
            order.email_account_id = order.email_account_id or request.email_account_id
            order.status = "campaign_setup"
            order.leads_collected = result.get("queued_messages", 0)
            log = list(order.action_log or [])
            log.append({"ts": datetime.utcnow().isoformat(), "msg": f"Campaign '{request.name}' created with {result.get('queued_messages', 0)} emails"})
            order.action_log = log
            db.commit()
            db.refresh(order)
            result["order_id"] = order.id
            logger.info("[CAMPAIGN] Linked campaign #%d to order #%d", result["campaign_id"], order.id)

            # Funnel: stages 8 & 9 — selecting styles and reaching campaign setup
            # both happen at this endpoint. Stage 8 only if styles are non-empty.
            if request.selected_styles:
                safe_mark_stage(db, str(current_user.id), "email_style_selected",
                                campaign_id=result["campaign_id"])
            safe_mark_stage(db, str(current_user.id), "campaign_setup",
                            campaign_id=result["campaign_id"],
                            email_account_id=request.email_account_id)
        except Exception as oe:
            logger.error("[CAMPAIGN] Failed to link funnel order: %s", oe)

        capture("campaign_created", str(current_user.id), {
            "campaign_id": result["campaign_id"],
            "generation_mode": "ai" if request.selected_styles else "template",
            "queued_emails": result.get("queued_messages", 0),
            "num_styles": len(request.selected_styles),
        })
        return {"status": "success", **result}
    except HTTPException:
        raise
    except ValueError as e:
        # Refund the reserved credits if campaign creation failed
        try:
            from database.models import UserCredit
            credits = db.query(UserCredit).filter_by(user_id=current_user.id).first()
            if credits:
                credits.used_credits = max(0, credits.used_credits - (request.lead_limit or 200))
                db.commit()
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Refund the reserved credits if campaign creation failed
        try:
            from database.models import UserCredit
            credits = db.query(UserCredit).filter_by(user_id=current_user.id).first()
            if credits:
                credits.used_credits = max(0, credits.used_credits - (request.lead_limit or 200))
                db.commit()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=str(e))


class EmailPreviewRequest(BaseModel):
    candidate_id: int
    selected_styles: list[str] = ["value_prop"]


@router.post("/preview-email")
async def preview_email(
    request: EmailPreviewRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate a sample email preview so the user can see quality before launching."""
    from database.models import Candidate, Lead
    from services.email_campaign.email_generator_service import (
        assign_style,
        generate_email_for_lead,
    )

    request.candidate_id = _resolve_effective_candidate(db, current_user.id, request.candidate_id)
    candidate = db.query(Candidate).filter_by(id=request.candidate_id, user_id=current_user.id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    # Pick one enriched lead to generate a sample email for
    sample_lead = (
        db.query(Lead)
        .filter(Lead.candidate_id == request.candidate_id, Lead.email.isnot(None), Lead.email_verified == True)
        .first()
    )

    if not sample_lead:
        # Fall back to any lead
        sample_lead = db.query(Lead).filter(Lead.candidate_id == request.candidate_id).first()

    if not sample_lead:
        raise HTTPException(status_code=404, detail="No leads found for preview")

    try:
        style = assign_style(sample_lead, request.selected_styles)
        # 25s timeout — gives Azure OpenAI enough time on slow days while still
        # failing fast with a clear message rather than hanging the frontend.
        subject, body = await asyncio.wait_for(
            asyncio.to_thread(generate_email_for_lead, sample_lead, candidate, style, current_user.name or ""),
            timeout=25.0,
        )
        return {
            "subject": subject,
            "body": body,
            "lead_name": sample_lead.name,
            "company": sample_lead.company,
            "style": style,
        }
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="Preview generation timed out. Your campaign will still work — emails are generated fresh per lead just before sending.",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Preview generation failed: {str(e)}")


def _schedule_top_lead_research(background_tasks: BackgroundTasks, result: dict) -> None:
    """Queue top-lead research after a campaign first enters 'running'.

    Runs as a BackgroundTask because it makes ~40-60s of blocking HTTP + LLM calls;
    invoked inline it would block the event loop of these `async def` handlers and
    starve /health. Skipped on resume (paused -> running) since the top lead was
    already researched at the original launch.
    """
    if result.get("new_status") == "running" and result.get("old_status") == "draft":
        from services.email_campaign.campaign_worker import research_top_lead_bg
        background_tasks.add_task(research_top_lead_bg, result["campaign_id"])


@router.post("/{campaign_id}/transition")
async def api_transition_campaign(
    campaign_id: int,
    request: CampaignTransitionRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Transition a campaign to a new state."""
    try:
        result = transition_campaign(db, campaign_id, request.target_status)
        _schedule_top_lead_research(background_tasks, result)
        return {"status": "success", **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{campaign_id}/send")
async def api_start_campaign(
    campaign_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Start sending emails for a campaign."""
    try:
        result = transition_campaign(db, campaign_id, "running")
        _schedule_top_lead_research(background_tasks, result)
        campaign = db.query(Campaign).filter_by(id=campaign_id).first()
        capture("campaign_started", str(current_user.id), {
            "campaign_id": campaign_id,
            "daily_limit": campaign.daily_limit if campaign else None,
        })
        return {"status": "success", **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class CampaignRescheduleRequest(BaseModel):
    user_timezone: str


@router.post("/{campaign_id}/reschedule")
async def api_reschedule_campaign(
    campaign_id: int,
    request: CampaignRescheduleRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update a campaign's timezone and recompute all future email schedules."""
    import pytz
    from services.email_campaign.campaign_worker import compute_campaign_schedule

    try:
        pytz.timezone(request.user_timezone)
    except pytz.exceptions.UnknownTimeZoneError:
        raise HTTPException(status_code=400, detail=f"Unknown timezone: {request.user_timezone}")

    campaign = db.query(Campaign).filter_by(id=campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    from database.models import Candidate
    candidate = db.query(Candidate).filter_by(id=campaign.candidate_id, user_id=current_user.id).first()
    if not candidate:
        raise HTTPException(status_code=403, detail="Not your campaign")

    if campaign.status not in ("running", "paused"):
        raise HTTPException(status_code=400, detail="Can only reschedule running or paused campaigns")

    campaign.user_timezone = request.user_timezone
    db.commit()

    compute_campaign_schedule(db, campaign_id)
    return {"status": "success", "user_timezone": request.user_timezone}


@router.get("/user/latest")
async def get_user_latest_campaign(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the user's most recent campaign (any status). Used to recover after page reload."""
    from database.models import Candidate
    # Find campaigns via user's candidates
    candidate_ids = [c.id for c in db.query(Candidate).filter_by(user_id=current_user.id).all()]
    if not candidate_ids:
        return {"campaign": None}

    campaign = (
        db.query(Campaign)
        .filter(Campaign.candidate_id.in_(candidate_ids))
        .order_by(Campaign.created_at.desc())
        .first()
    )
    if not campaign:
        return {"campaign": None}

    return {
        "campaign": {
            "id": campaign.id,
            "name": campaign.name,
            "status": campaign.status,
            "created_at": campaign.created_at.isoformat() if campaign.created_at else None,
        }
    }


@router.get("/{campaign_id}")
async def get_campaign(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get campaign details."""
    campaign = db.query(Campaign).filter_by(id=campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    return {
        "id": campaign.id,
        "name": campaign.name,
        "status": campaign.status,
        "subject_template": campaign.subject_template,
        "body_template": campaign.body_template,
        "daily_limit": campaign.daily_limit,
        "created_at": campaign.created_at.isoformat() if campaign.created_at else None,
    }


@router.get("/{campaign_id}/metrics")
async def get_campaign_analytics(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get campaign analytics and metrics."""
    try:
        metrics = get_campaign_metrics(db, campaign_id)
        return {"status": "success", **metrics}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


class TestEmailOverride(BaseModel):
    lead_index: int
    override_email: str


class TestLaunchRequest(BaseModel):
    candidate_id: int
    email_account_id: int
    overrides: Optional[list[TestEmailOverride]] = None
    selected_styles: list[str] = ["value_prop"]


# ── Postgres-backed store for async test-launch jobs ──────────────────────────
# Persisted (not in-memory) so the replica running the background thread and the
# replica serving the status poll agree. The service runs multiple replicas, and
# an in-memory dict made GET /test-launch/{id}/status 404 ~half the time.
import uuid
import threading
import json as _json
from sqlalchemy import text as _sql_text


def _save_test_launch_job(db, job_id: str, job: dict) -> None:
    db.execute(
        _sql_text(
            "INSERT INTO test_launch_jobs (job_id, data, updated_at) "
            "VALUES (:jid, CAST(:data AS jsonb), now()) "
            "ON CONFLICT (job_id) DO UPDATE SET data = CAST(:data AS jsonb), updated_at = now()"
        ),
        {"jid": job_id, "data": _json.dumps(job)},
    )
    db.commit()


def _load_test_launch_job(db, job_id: str):
    row = db.execute(
        _sql_text("SELECT data FROM test_launch_jobs WHERE job_id = :jid"),
        {"jid": job_id},
    ).fetchone()
    if not row:
        return None
    data = row[0]
    return data if isinstance(data, dict) else _json.loads(data)


def _cleanup_old_test_launch_jobs(db) -> None:
    """Best-effort purge of finished jobs so the table doesn't grow unbounded."""
    try:
        db.execute(_sql_text("DELETE FROM test_launch_jobs WHERE updated_at < now() - interval '2 days'"))
        db.commit()
    except Exception:
        db.rollback()


@router.post("/test-launch/preview")
async def test_launch_preview(
    request: TestLaunchRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Preview the 5 test emails without sending. Returns lead info so user can set overrides."""
    from database.models import Candidate, Lead
    from services.email_campaign.email_generator_service import assign_style, generate_email_for_lead

    request.candidate_id = _resolve_effective_candidate(db, current_user.id, request.candidate_id)
    candidate = db.query(Candidate).filter_by(
        id=request.candidate_id, user_id=current_user.id
    ).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    leads = _test_leads(db, request.candidate_id, 5)
    if not leads:
        raise HTTPException(status_code=400, detail="No leads found yet. Run lead discovery first.")

    emails = []
    for idx, lead in enumerate(leads):
        try:
            style = assign_style(lead, request.selected_styles)
            subject, body = await asyncio.to_thread(generate_email_for_lead, lead, candidate, style)
        except Exception:
            subject, body = f"Intro from {candidate.parsed_json.get('personal_info', {}).get('name', 'me')}", "Hi there..."
        emails.append({
            "index": idx,
            "lead_name": lead.name,
            "lead_company": lead.company,
            "original_email": lead.email,
            "subject": subject,
            "body": body,
        })

    return {"emails": emails}


def _run_test_launch_in_background(
    job_id: str,
    candidate_id: int,
    email_account_id: int,
    selected_styles: list,
    override_map: dict,
    user_id: str,
):
    """Background thread: send test emails, updating _test_launch_jobs leads in real-time."""
    from database.session import SessionLocal
    from database.models import Candidate, Lead, EmailAccount
    from services.email_campaign.email_generator_service import assign_style, generate_email_for_lead
    from services.email_campaign.gmail_send_service import send_gmail_email, _refresh_token_sync

    db = SessionLocal()
    job = _load_test_launch_job(db, job_id) or {
        "status": "processing", "progress": "Starting...", "leads": [],
        "total": 0, "emails_sent": 0, "emails_failed": 0, "error": "",
    }

    def _persist():
        _save_test_launch_job(db, job_id, job)

    try:
        candidate = db.query(Candidate).filter_by(id=candidate_id, user_id=user_id).first()
        account = db.query(EmailAccount).filter_by(id=email_account_id).first()

        if not candidate or not account or not account.access_token:
            job["status"] = "failed"
            job["error"] = "Candidate or email account not found"
            _persist()
            return

        leads_db = _test_leads(db, candidate_id, 5)
        if not leads_db:
            job["status"] = "failed"
            job["error"] = "No leads found yet. Run lead discovery first."
            _persist()
            return

        access_token = _refresh_token_sync(account, db)

        # Test emails go to the user's own inbox. Use the override if set, else the
        # lead's email, else fall back to the connected account so the test always
        # delivers somewhere valid (raw, un-enriched leads have no email).
        job["leads"] = []
        for idx, lead in enumerate(leads_db):
            to_email = override_map.get(idx) or lead.email or account.email_address
            job["leads"].append({
                "lead_name": lead.name or "Unknown",
                "company": lead.company or "",
                "email": to_email,
                "status": "queued",
                "subject": "",
                "schedule_offset": idx * 20,
            })
        job["total"] = len(leads_db)
        _persist()

        for idx, lead in enumerate(leads_db):
            to_email = override_map.get(idx) or lead.email or account.email_address
            job["progress"] = f"Sending {idx + 1}/{len(leads_db)}"
            job["leads"][idx]["status"] = "sending"
            _persist()

            try:
                style = assign_style(lead, selected_styles)
                subject, body = generate_email_for_lead(lead, candidate, style)
                if idx in override_map:
                    subject = f"[TEST] {subject}"

                logger.info("[TEST_LAUNCH] Sending email %d/%d to %s", idx + 1, len(leads_db), to_email)
                send_gmail_email(access_token=access_token, to_email=to_email, subject=subject, body=body, from_email=account.email_address)

                job["leads"][idx].update({"status": "sent", "subject": subject})
                job["emails_sent"] += 1

            except Exception as e:
                logger.error("[TEST_LAUNCH] Failed email %d to %s: %s", idx + 1, to_email, e, exc_info=True)
                job["leads"][idx].update({"status": "failed", "error": str(e)})
                job["emails_failed"] += 1
            _persist()

            if idx < len(leads_db) - 1:
                time.sleep(20)

        job["status"] = "completed"
        _persist()
        logger.info("[TEST_LAUNCH] Job %s completed: %d sent, %d failed", job_id, job["emails_sent"], job["emails_failed"])

    except Exception as e:
        logger.error("[TEST_LAUNCH] Job %s crashed: %s", job_id, e, exc_info=True)
        job["status"] = "failed"
        job["error"] = str(e)
        try:
            _persist()
        except Exception:
            pass
    finally:
        db.close()


@router.post("/test-launch")
async def test_launch_campaign(
    request: TestLaunchRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Launch test emails in background. Returns a job_id to poll for results.

    This endpoint returns immediately to prevent blocking the event loop
    and causing health probe failures (which was causing 503 errors).
    Use GET /test-launch/{job_id}/status to poll for results.
    """
    from database.models import Candidate, Lead, EmailAccount

    # Validate inputs before spawning background job
    request.candidate_id = _resolve_effective_candidate(db, current_user.id, request.candidate_id)
    candidate = db.query(Candidate).filter_by(
        id=request.candidate_id, user_id=current_user.id
    ).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    account = db.query(EmailAccount).filter_by(id=request.email_account_id).first()
    if not account or not account.access_token:
        raise HTTPException(status_code=400, detail="Gmail account not connected or token missing")

    leads_preview = _test_leads(db, request.candidate_id, 5)
    if not leads_preview:
        raise HTTPException(status_code=400, detail="No leads found yet. Run lead discovery first.")

    # Build override map
    override_map = {}
    if request.overrides:
        for ov in request.overrides:
            override_map[ov.lead_index] = ov.override_email

    initial_leads = []
    for idx, lead in enumerate(leads_preview):
        to_email = override_map.get(idx) or lead.email or account.email_address
        initial_leads.append({
            "lead_name": lead.name or "Unknown",
            "company": lead.company or "",
            "email": to_email,
            "status": "queued",
            "subject": "",
            "schedule_offset": idx * 20,
        })

    # Create job with pre-populated leads and spawn background thread
    import math
    from datetime import datetime as _dt
    job_id = str(uuid.uuid4())[:8]
    _cleanup_old_test_launch_jobs(db)
    _save_test_launch_job(db, job_id, {
        "status": "processing",
        "progress": "Starting...",
        "started_at": _dt.utcnow().isoformat(),
        "leads": initial_leads,
        "total": len(initial_leads),
        "emails_sent": 0,
        "emails_failed": 0,
        "error": "",
    })

    thread = threading.Thread(
        target=_run_test_launch_in_background,
        args=(job_id, request.candidate_id, request.email_account_id,
              request.selected_styles, override_map, str(current_user.id)),
        daemon=True,
    )
    thread.start()

    logger.info("[TEST_LAUNCH] Job %s started in background for user %s", job_id, current_user.id)
    capture("test_launch_started", str(current_user.id), {
        "job_id": job_id,
        "num_test_emails": len(initial_leads),
        "num_styles": len(request.selected_styles),
    })

    return {
        "status": "processing",
        "job_id": job_id,
        "test_mode": True,
        "leads": initial_leads,
        "total": len(initial_leads),
    }


@router.get("/test-launch/{job_id}/status")
async def test_launch_status(job_id: str, db: Session = Depends(get_db)):
    """Poll for test-launch job results with per-lead status."""
    job = _load_test_launch_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return {
        "job_id": job_id,
        "status": job["status"],
        "progress": job.get("progress", ""),
        "test_mode": True,
        "started_at": job.get("started_at", ""),
        "total": job.get("total", 0),
        "emails_sent": job.get("emails_sent", 0),
        "emails_failed": job.get("emails_failed", 0),
        "leads": job.get("leads", []),
        "error": job.get("error", ""),
    }


@router.get("/{campaign_id}/emails")
async def get_campaign_emails(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return all emails for a campaign with lead details and schedule info."""
    from database.models import EmailSent, Lead

    from database.models import Candidate
    campaign = db.query(Campaign).filter_by(id=campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    # Verify ownership through candidate
    candidate = db.query(Candidate).filter_by(id=campaign.candidate_id, user_id=current_user.id).first()
    if not candidate:
        raise HTTPException(status_code=403, detail="Not authorized")

    emails = (
        db.query(EmailSent)
        .filter(EmailSent.campaign_id == campaign_id)
        .order_by(func.coalesce(EmailSent.scheduled_at, EmailSent.created_at).asc())
        .all()
    )

    result = []
    for email in emails:
        lead = db.query(Lead).filter_by(id=email.lead_id).first() if email.lead_id else None
        result.append({
            "id": email.id,
            "lead_name": lead.name if lead else "Unknown",
            "lead_company": lead.company if lead else "",
            "lead_title": lead.title if lead else "",
            "to_email": email.to_email,
            "subject": email.subject,
            "body": email.body,
            "status": email.status,
            "enrichment_status": email.enrichment_status,
            "assigned_style": email.assigned_style,
            "scheduled_at": email.scheduled_at.isoformat() if email.scheduled_at else None,
            "sent_at": email.sent_at.isoformat() if email.sent_at else None,
            "reply_text": email.reply_text,
            "reply_sentiment": email.reply_sentiment,
            "reply_received_at": email.reply_received_at.isoformat() if email.reply_received_at else None,
            "bounce_reason": email.bounce_reason,
            "is_test": email.is_test or False,
            "followup_number": email.followup_number or 0,
            "parent_email_id": email.parent_email_id,
        })

    return {"emails": result}


# ── Send Test Emails ─────────────────────────────────────────────────────────

@router.post("/{campaign_id}/send-test-emails")
async def send_test_emails(
    campaign_id: int,
    payload: SendTestEmailsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Insert test email rows into a campaign's lead list.

    Creates EmailSent rows with is_test=True, scheduled_at=2 minutes from now,
    and status='queued'. The normal campaign worker picks these up and sends them
    through the exact same pipeline as real campaign emails.

    This is a permanent feature — lets the user test reply detection, sentiment
    classification, and the full send pipeline using their own email addresses.
    """
    from database.models import EmailSent, Candidate
    from datetime import timedelta

    # Verify campaign exists and user owns it
    campaign = db.query(Campaign).filter_by(id=campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    candidate = db.query(Candidate).filter_by(id=campaign.candidate_id, user_id=current_user.id).first()
    if not candidate:
        raise HTTPException(status_code=403, detail="Not authorized")

    if campaign.status not in ("running", "completed", "paused"):
        raise HTTPException(status_code=400, detail="Campaign must be running, paused, or completed to send test emails")

    if not payload.recipients or len(payload.recipients) > 10:
        raise HTTPException(status_code=400, detail="Provide 1-10 test recipients")

    # Schedule 2 minutes from now so the worker picks it up immediately
    send_at = datetime.utcnow() + timedelta(minutes=2)

    # Extract candidate name from parsed resume JSON (Candidate model has no `name` field)
    candidate_name = ""
    if candidate.parsed_json and isinstance(candidate.parsed_json, dict):
        candidate_name = candidate.parsed_json.get("personal_info", {}).get("name", "")

    created_emails = []
    for recipient in payload.recipients:
        # Test rows have no lead to infer a band from; 'small' is the neutral default.
        style = "small"
        email_row = EmailSent(
            campaign_id=campaign_id,
            lead_id=None,
            to_email=recipient.email,
            subject=f"[Test] Outreach from {candidate_name or 'your campaign'}",
            body=(
                f"Hi {recipient.first_name},\n\n"
                f"This is a test email from your outreach campaign.\n\n"
                f"If you received this, the campaign pipeline is working correctly. "
                f"Reply to this email with any message to test reply detection and sentiment analysis.\n\n"
                f"Best regards"
            ),
            status="queued",
            enrichment_status="skipped",
            assigned_style=style,
            scheduled_at=send_at,
            is_test=True,
        )
        db.add(email_row)
        db.flush()
        created_emails.append({
            "id": email_row.id,
            "to_email": recipient.email,
            "first_name": recipient.first_name,
            "company": recipient.company,
            "scheduled_at": send_at.isoformat(),
            "status": "queued",
        })

    db.commit()
    logger.info("[TEST_EMAILS] Created %d test emails for campaign %d", len(created_emails), campaign_id)

    return {
        "message": f"{len(created_emails)} test email(s) scheduled — sending in ~2 minutes",
        "emails": created_emails,
    }


# ── Cancel Campaign (with credit refund) ───────────────────────────────────

@router.post("/{campaign_id}/cancel")
async def cancel_campaign(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cancel a running campaign and refund credits for un-enriched leads."""
    from database.models import EmailSent, OutreachOrder, Candidate
    from sqlalchemy import func
    from api.routes_payment import refund_credits

    campaign = db.query(Campaign).filter_by(id=campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Verify ownership
    candidate = db.query(Candidate).filter_by(id=campaign.candidate_id, user_id=current_user.id).first()
    if not candidate:
        raise HTTPException(status_code=403, detail="Not authorized")

    if campaign.status not in ("draft", "running", "paused"):
        raise HTTPException(status_code=400, detail=f"Cannot cancel campaign in '{campaign.status}' status")

    # Count un-enriched emails (these never consumed Apollo credits)
    pending_count = db.query(func.count(EmailSent.id)).filter(
        EmailSent.campaign_id == campaign_id,
        EmailSent.enrichment_status == "pending",
    ).scalar() or 0

    # Mark pending emails as skipped
    db.query(EmailSent).filter(
        EmailSent.campaign_id == campaign_id,
        EmailSent.status.in_(["pending_enrichment", "queued"]),
    ).update({
        EmailSent.status: "failed",
        EmailSent.error_message: "Campaign cancelled",
    }, synchronize_session="fetch")

    campaign.status = "completed"

    # Refund credits for un-enriched leads
    if pending_count > 0:
        refund_credits(db, current_user.id, pending_count)

    # Update outreach order
    order = db.query(OutreachOrder).filter_by(campaign_id=campaign_id).first()
    if order:
        order.status = "completed"
        order.credits_refunded = (order.credits_refunded or 0) + pending_count
        from datetime import datetime
        log = list(order.action_log or [])
        log.append({"ts": datetime.utcnow().isoformat(), "msg": f"Campaign cancelled, {pending_count} credits refunded"})
        order.action_log = log
        order.updated_at = datetime.utcnow()

    db.commit()

    logger.info("[CAMPAIGN] Campaign %d cancelled by user %s, refunded %d credits",
                campaign_id, current_user.id, pending_count)
    capture("campaign_cancelled", str(current_user.id), {
        "campaign_id": campaign_id,
        "credits_refunded": pending_count,
    })

    return {
        "status": "cancelled",
        "credits_refunded": pending_count,
    }


# ── Internal Worker Endpoints (no auth — cluster-only) ─────────────────────

@router.post("/worker/send-ready")
def worker_send_ready():
    """Internal endpoint called by job-outreach-worker goroutine every 30s.

    Runs the 3-phase JIT cycle: enrich upcoming → generate content → send ready.
    No auth required because this is only accessible within the k8s cluster.

    NOTE: this MUST be `def`, not `async def`. The cycle does blocking HTTP
    calls (Azure OpenAI takes 5-15s) and synchronous DB queries. As an
    `async def` it ran the cycle on the FastAPI event loop, starving every
    other request including /health — which caused 58 liveness-probe
    failures and CrashLoopBackOff on 2026-05-18. FastAPI runs sync handlers
    on its threadpool, leaving the event loop free for /health and other
    endpoints.
    """
    from services.email_campaign.campaign_worker import _process_cycle

    try:
        result = _process_cycle()
        return result
    except Exception as e:
        logger.error("[WORKER] send-ready failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
