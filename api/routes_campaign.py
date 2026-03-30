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
        # Auto-create an outreach order for tracking
        try:
            from database.models import OutreachOrder
            from datetime import datetime
            order = OutreachOrder(
                user_id=current_user.id,
                candidate_id=request.candidate_id,
                campaign_id=result["campaign_id"],
                email_account_id=request.email_account_id,
                status="campaign_setup",
                leads_collected=result.get("queued_messages", 0),
            )
            order.action_log = [{"ts": datetime.utcnow().isoformat(), "msg": f"Campaign '{request.name}' created with {result.get('queued_messages', 0)} emails"}]
            db.add(order)
            db.commit()
            db.refresh(order)
            result["order_id"] = order.id
            logger.info("[CAMPAIGN] Auto-created order #%d for campaign #%d", order.id, result["campaign_id"])
        except Exception as oe:
            logger.error("[CAMPAIGN] Failed to auto-create order: %s", oe)

        capture("campaign_created", str(current_user.id), {
            "campaign_id": result["campaign_id"],
            "generation_mode": "ai" if request.selected_styles else "template",
            "queued_emails": result.get("queued_messages", 0),
            "num_styles": len(request.selected_styles),
        })
        return {"status": "success", **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
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
        # Hard 12s timeout — prevents the 30s frontend timeout being hit 3x (= 90s hang).
        # If Azure OpenAI is slow, we fail fast with a clear message.
        subject, body = await asyncio.wait_for(
            asyncio.to_thread(generate_email_for_lead, sample_lead, candidate, style),
            timeout=12.0,
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


@router.post("/{campaign_id}/transition")
async def api_transition_campaign(
    campaign_id: int,
    request: CampaignTransitionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Transition a campaign to a new state."""
    try:
        result = transition_campaign(db, campaign_id, request.target_status)
        return {"status": "success", **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{campaign_id}/send")
async def api_start_campaign(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Start sending emails for a campaign."""
    try:
        result = transition_campaign(db, campaign_id, "running")
        campaign = db.query(Campaign).filter_by(id=campaign_id).first()
        capture("campaign_started", str(current_user.id), {
            "campaign_id": campaign_id,
            "daily_limit": campaign.daily_limit if campaign else None,
        })
        return {"status": "success", **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


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


# ── In-memory store for async test-launch jobs ────────────────────────────────
import uuid
import threading
_test_launch_jobs: dict = {}  # job_id -> {status, results, ...}


@router.post("/test-launch/preview")
async def test_launch_preview(
    request: TestLaunchRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Preview the 5 test emails without sending. Returns lead info so user can set overrides."""
    from database.models import Candidate, Lead
    from services.email_campaign.email_generator_service import assign_style, generate_email_for_lead

    candidate = db.query(Candidate).filter_by(
        id=request.candidate_id, user_id=current_user.id
    ).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    leads = (
        db.query(Lead)
        .filter(Lead.candidate_id == request.candidate_id, Lead.email.isnot(None), Lead.email_verified == True)
        .limit(5).all()
    )
    if not leads:
        raise HTTPException(status_code=400, detail="No leads with verified emails found")

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
    job = _test_launch_jobs[job_id]
    try:
        candidate = db.query(Candidate).filter_by(id=candidate_id, user_id=user_id).first()
        account = db.query(EmailAccount).filter_by(id=email_account_id).first()

        if not candidate or not account or not account.access_token:
            job["status"] = "failed"
            job["error"] = "Candidate or email account not found"
            return

        leads_db = (
            db.query(Lead)
            .filter(Lead.candidate_id == candidate_id, Lead.email.isnot(None), Lead.email_verified == True)
            .limit(5).all()
        )
        if not leads_db:
            job["status"] = "failed"
            job["error"] = "No leads with verified emails found"
            return

        access_token = _refresh_token_sync(account, db)

        # Populate the leads list with real data (the POST handler pre-populated placeholders)
        job["leads"] = []
        for idx, lead in enumerate(leads_db):
            to_email = override_map.get(idx, lead.email)
            job["leads"].append({
                "lead_name": lead.name or "Unknown",
                "company": lead.company or "",
                "email": to_email,
                "status": "queued",
                "subject": "",
                "schedule_offset": idx * 20,
            })
        job["total"] = len(leads_db)

        for idx, lead in enumerate(leads_db):
            to_email = override_map.get(idx, lead.email)
            job["progress"] = f"Sending {idx + 1}/{len(leads_db)}"
            job["leads"][idx]["status"] = "sending"

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

            if idx < len(leads_db) - 1:
                time.sleep(20)

        job["status"] = "completed"
        logger.info("[TEST_LAUNCH] Job %s completed: %d sent, %d failed", job_id, job["emails_sent"], job["emails_failed"])

    except Exception as e:
        logger.error("[TEST_LAUNCH] Job %s crashed: %s", job_id, e, exc_info=True)
        job["status"] = "failed"
        job["error"] = str(e)
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
    candidate = db.query(Candidate).filter_by(
        id=request.candidate_id, user_id=current_user.id
    ).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    account = db.query(EmailAccount).filter_by(id=request.email_account_id).first()
    if not account or not account.access_token:
        raise HTTPException(status_code=400, detail="Gmail account not connected or token missing")

    leads_count = (
        db.query(Lead)
        .filter(Lead.candidate_id == request.candidate_id, Lead.email.isnot(None), Lead.email_verified == True)
        .count()
    )
    if leads_count == 0:
        raise HTTPException(status_code=400, detail="No leads with verified emails found")

    # Build override map
    override_map = {}
    if request.overrides:
        for ov in request.overrides:
            override_map[ov.lead_index] = ov.override_email

    # Pre-load leads so the dashboard can show them immediately
    leads_preview = (
        db.query(Lead)
        .filter(Lead.candidate_id == request.candidate_id, Lead.email.isnot(None), Lead.email_verified == True)
        .limit(5).all()
    )

    initial_leads = []
    for idx, lead in enumerate(leads_preview):
        to_email = override_map.get(idx, lead.email)
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
    _test_launch_jobs[job_id] = {
        "status": "processing",
        "progress": "Starting...",
        "started_at": _dt.utcnow().isoformat(),
        "leads": initial_leads,
        "total": len(initial_leads),
        "emails_sent": 0,
        "emails_failed": 0,
        "error": "",
    }

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
async def test_launch_status(job_id: str):
    """Poll for test-launch job results with per-lead status."""
    job = _test_launch_jobs.get(job_id)
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
        style = (campaign.selected_styles[0] if campaign.selected_styles else "warm_intro")
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
async def worker_send_ready():
    """Internal endpoint called by job-outreach-worker goroutine every 30s.

    Runs the 3-phase JIT cycle: enrich upcoming → generate content → send ready.
    No auth required because this is only accessible within the k8s cluster.
    """
    from services.email_campaign.campaign_worker import _process_cycle

    try:
        result = _process_cycle()
        return result
    except Exception as e:
        logger.error("[WORKER] send-ready failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
