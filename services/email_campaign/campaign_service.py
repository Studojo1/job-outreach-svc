"""Campaign Service — Campaign lifecycle management.

State machine: draft → running → paused → completed

Handles campaign creation, state transitions, email queuing,
and metrics computation. Supports both AI-generated and template-based emails.
"""

from datetime import datetime
from typing import Dict, Any, Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from database.models import Campaign, EmailSent, Lead, EmailAccount, Candidate
from core.logger import get_logger
from core.metrics import CAMPAIGNS_RUNNING, EMAILS_SENT_TOTAL
from services.email_campaign.email_generator_service import (
    assign_style,
    generate_email_for_lead,
)

logger = get_logger(__name__)

# Valid state transitions
VALID_TRANSITIONS = {
    "draft": ["running"],
    "running": ["paused", "completed"],
    "paused": ["running", "completed"],
    "completed": [],
}


def create_campaign(
    db: Session,
    user_id: str,
    name: str,
    email_account_id: int,
    candidate_id: int,
    daily_limit: int = 20,
    subject_template: str = "",
    body_template: str = "",
    selected_styles: Optional[list] = None,
    user_timezone: str = "Asia/Kolkata",
) -> Dict[str, Any]:
    """Create a new email campaign and queue messages for enriched leads.

    Generates AI-powered, personalized emails for each lead using the selected styles.
    If selected_styles is provided, emails are fully AI-generated with auto-style assignment.
    If not provided, falls back to template substitution (legacy mode).

    Args:
        db: Database session.
        user_id: Owner user ID.
        name: Campaign name.
        email_account_id: Gmail account to send from.
        candidate_id: The candidate whose leads to pull from.
        daily_limit: Max emails per day.
        subject_template: Email subject template (legacy, used if selected_styles not provided).
        body_template: Email body template (legacy, used if selected_styles not provided).
        selected_styles: List of email styles to use for AI generation (e.g., ["warm_intro", "value_prop"]).

    Returns:
        Dict with campaign details and queued message count.
    """
    logger.info(
        "[CAMPAIGN] Creating campaign: %s (account=%d, candidate=%d, styles=%s)",
        name, email_account_id, candidate_id, selected_styles
    )

    # Verify email account exists
    account = db.query(EmailAccount).filter_by(id=email_account_id).first()
    if not account:
        raise ValueError(f"Email account {email_account_id} not found")

    # Fetch candidate for AI generation
    candidate = db.query(Candidate).filter_by(id=candidate_id).first()
    if not candidate:
        raise ValueError(f"Candidate {candidate_id} not found")

    # Create campaign
    campaign = Campaign(
        candidate_id=candidate_id,
        email_account_id=email_account_id,
        name=name,
        status="draft",
        subject_template=subject_template,
        body_template=body_template,
        user_timezone=user_timezone,
    )
    db.add(campaign)
    db.flush()

    # Find enriched leads (those with verified emails)
    enriched_leads = (
        db.query(Lead)
        .filter(
            Lead.candidate_id == candidate_id,
            Lead.email.isnot(None),
            Lead.email_verified == True,
        )
        .all()
    )

    queued = 0
    failed = 0

    # Determine generation mode
    use_ai_generation = selected_styles and len(selected_styles) > 0

    for lead in enriched_leads:
        try:
            if use_ai_generation:
                # AI-powered email generation
                assigned_style = assign_style(lead, selected_styles)
                subject, body = generate_email_for_lead(lead, candidate, assigned_style)

                email_sent = EmailSent(
                    campaign_id=campaign.id,
                    lead_id=lead.id,
                    to_email=lead.email,
                    subject=subject,
                    body=body,
                    assigned_style=assigned_style,
                    status="queued",
                )
                db.add(email_sent)
                queued += 1

            else:
                # Legacy: Simple template substitution
                subject = subject_template.replace("{name}", lead.name or "")
                subject = subject.replace("{company}", lead.company or "")
                body = body_template.replace("{name}", lead.name or "")
                body = body.replace("{company}", lead.company or "")
                body = body.replace("{title}", lead.title or "")

                email_sent = EmailSent(
                    campaign_id=campaign.id,
                    lead_id=lead.id,
                    to_email=lead.email,
                    subject=subject,
                    body=body,
                    status="queued",
                )
                db.add(email_sent)
                queued += 1

        except Exception as e:
            logger.error("[CAMPAIGN] Failed to generate email for lead %s: %s", lead.name, str(e))
            failed += 1
            continue

    db.commit()

    logger.info(
        "[CAMPAIGN] Campaign #%d created: %d messages queued, %d failed",
        campaign.id, queued, failed
    )

    return {
        "campaign_id": campaign.id,
        "name": name,
        "status": "draft",
        "queued_messages": queued,
        "generation_mode": "ai" if use_ai_generation else "template",
        "failed_generations": failed,
        "email_account": account.email_address,
    }


def transition_campaign(db: Session, campaign_id: int, target_status: str) -> Dict[str, Any]:
    """Transition a campaign to a new state.

    Raises:
        ValueError: If transition is invalid.
    """
    campaign = db.query(Campaign).filter_by(id=campaign_id).first()
    if not campaign:
        raise ValueError(f"Campaign {campaign_id} not found")

    valid = VALID_TRANSITIONS.get(campaign.status, [])
    if target_status not in valid:
        raise ValueError(
            f"Invalid transition: {campaign.status} -> {target_status}. "
            f"Valid transitions: {valid}"
        )

    old_status = campaign.status
    campaign.status = target_status
    db.commit()

    logger.info("[CAMPAIGN] Campaign #%d: %s -> %s", campaign_id, old_status, target_status,
                extra={"event": "campaign_state_change", "campaign_id": campaign_id})

    if target_status == "running":
        CAMPAIGNS_RUNNING.inc()
    elif old_status == "running":
        CAMPAIGNS_RUNNING.dec()

    # Record pause time when pausing
    if old_status == "running" and target_status == "paused":
        campaign.paused_at = datetime.utcnow()
        db.commit()
        logger.info("[CAMPAIGN] Paused campaign #%d, paused_at=%s", campaign_id, campaign.paused_at)

    # Queue campaign bootstrap (scheduler initialization) if transitioning to "running"
    if target_status == "running":
        # Safety: check that there are actually queued emails before launching
        queued_count = db.query(func.count(EmailSent.id)).filter(
            EmailSent.campaign_id == campaign_id,
            EmailSent.status == "queued"
        ).scalar() or 0

        if queued_count == 0:
            # Revert status — nothing to send
            campaign.status = old_status
            db.commit()
            raise ValueError("Cannot launch campaign: no emails in queue. Ensure leads have verified emails.")

        # Pre-compute schedule timestamps for all queued emails
        from services.email_campaign.campaign_worker import compute_campaign_schedule
        compute_campaign_schedule(db, campaign_id)
        logger.info("[CAMPAIGN] Computed schedule for campaign #%d (queued=%d)",
                    campaign_id, queued_count)

    # Handle pause → running (resume): shift schedules forward by pause duration
    if old_status == "paused" and target_status == "running" and campaign.paused_at:
        pause_duration = (datetime.utcnow() - campaign.paused_at).total_seconds()
        from services.email_campaign.campaign_worker import shift_schedule_forward
        shift_schedule_forward(db, campaign_id, pause_duration)
        campaign.paused_at = None
        db.commit()
        logger.info("[CAMPAIGN] Resumed campaign #%d, shifted schedule by %.0f seconds",
                    campaign_id, pause_duration)

    # Sync order status
    try:
        from database.models import OutreachOrder
        order = db.query(OutreachOrder).filter_by(campaign_id=campaign_id).first()
        if order:
            order_status_map = {
                "running": "campaign_running",
                "paused": "campaign_setup",
                "completed": "completed",
            }
            new_order_status = order_status_map.get(target_status)
            if new_order_status and order.status != new_order_status:
                order.status = new_order_status
                log = list(order.action_log or [])
                log.append({"ts": datetime.utcnow().isoformat(), "msg": f"Campaign {old_status} -> {target_status}"})
                order.action_log = log
                order.updated_at = datetime.utcnow()
                db.commit()
    except Exception as e:
        logger.error("[CAMPAIGN] Failed to sync order status: %s", e)

    return {
        "campaign_id": campaign_id,
        "old_status": old_status,
        "new_status": target_status,
    }


def get_campaign_metrics(db: Session, campaign_id: int) -> Dict[str, Any]:
    """Compute metrics for a campaign."""
    campaign = db.query(Campaign).filter_by(id=campaign_id).first()
    if not campaign:
        raise ValueError(f"Campaign {campaign_id} not found")

    total = db.query(func.count(EmailSent.id)).filter(
        EmailSent.campaign_id == campaign_id
    ).scalar() or 0

    sent = db.query(func.count(EmailSent.id)).filter(
        EmailSent.campaign_id == campaign_id,
        EmailSent.status.in_(["sent", "replied"]),
    ).scalar() or 0

    failed = db.query(func.count(EmailSent.id)).filter(
        EmailSent.campaign_id == campaign_id,
        EmailSent.status == "failed",
    ).scalar() or 0

    replied = db.query(func.count(EmailSent.id)).filter(
        EmailSent.campaign_id == campaign_id,
        EmailSent.status == "replied",
    ).scalar() or 0

    queued = db.query(func.count(EmailSent.id)).filter(
        EmailSent.campaign_id == campaign_id,
        EmailSent.status.in_(["queued", "scheduled"]),
    ).scalar() or 0

    reply_rate = (replied / sent * 100) if sent > 0 else 0.0

    metrics = {
        "campaign_id": campaign_id,
        "campaign_name": campaign.name,
        "status": campaign.status,
        "emails_total": total,
        "emails_queued": queued,
        "emails_sent": sent,
        "emails_failed": failed,
        "emails_replied": replied,
        "reply_rate": round(reply_rate, 2),
    }

    logger.info("[CAMPAIGN] Metrics for #%d: %s", campaign_id, metrics)

    return metrics
