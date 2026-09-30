"""Auto-replenishment: swap in a fresh lead when an email bounces or
exhausts enrichment retries due to "Apollo could not find this person".

A campaign starts with N emails materialized into emails_sent rows. When one
of those goes bad (recipient bounces, or Apollo can't find an email after
3 attempts), this module promotes the next highest-scored unenriched lead
from the user's pool into a new emails_sent row.

Rules:
  - Cap per campaign: 25% of the initial materialized lead count
  - Never replace a replacement (replacement_for_id IS NULL on the trigger row)
  - Pick by LeadScore.overall_score DESC, excluding leads already in the campaign
  - New row gets scheduled_at = a new slot at the tail of the campaign schedule
  - Writes an audit entry to OutreachOrder.action_log if present
"""

from __future__ import annotations

from datetime import datetime, timedelta
import math
from typing import Literal, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.logger import get_logger
from database.models import (
    Campaign,
    EmailSent,
    Lead,
    LeadScore,
)

logger = get_logger(__name__)

# Free auto-replenishment is capped at 25% of the campaign's initial lead count
REPLACEMENT_CAP_PERCENT = 0.25

ReplacementReason = Literal["bounce", "enrichment_exhausted"]


def _campaign_initial_size(db: Session, campaign_id: int) -> int:
    """Initial (non-replacement) email count for the campaign — the denominator
    for the 25% cap. Counts only Touch 1 / initial emails, excludes follow-ups
    and replacements."""
    return (
        db.query(func.count(EmailSent.id))
        .filter(
            EmailSent.campaign_id == campaign_id,
            (EmailSent.followup_number == 0) | (EmailSent.followup_number.is_(None)),
            EmailSent.replacement_for_id.is_(None),
        )
        .scalar()
    ) or 0


def _campaign_replacements_used(db: Session, campaign_id: int) -> int:
    """How many replacement rows already exist for this campaign."""
    return (
        db.query(func.count(EmailSent.id))
        .filter(
            EmailSent.campaign_id == campaign_id,
            EmailSent.replacement_for_id.isnot(None),
        )
        .scalar()
    ) or 0


def _pick_next_lead(db: Session, campaign: Campaign) -> Optional[Lead]:
    """Highest-scored lead for this candidate that has never been in this
    campaign and hasn't already exhausted enrichment retries."""
    used_lead_ids_sq = (
        db.query(EmailSent.lead_id)
        .filter(EmailSent.campaign_id == campaign.id)
        .subquery()
    )
    lead = (
        db.query(Lead)
        .outerjoin(LeadScore, LeadScore.lead_id == Lead.id)
        .filter(
            Lead.candidate_id == campaign.candidate_id,
            Lead.id.notin_(used_lead_ids_sq),
            (Lead.enrichment_fail_count == None) | (Lead.enrichment_fail_count < 3),
        )
        .order_by(LeadScore.overall_score.desc().nullslast(), Lead.id.asc())
        .first()
    )
    return lead


def _next_schedule_slot(db: Session, campaign_id: int) -> datetime:
    """Append the replacement after the last scheduled email in the campaign,
    with a small random-feeling gap. We keep it simple: max(scheduled_at) + 50m,
    or 5 minutes from now if no schedule yet (campaign just started)."""
    max_scheduled = (
        db.query(func.max(EmailSent.scheduled_at))
        .filter(EmailSent.campaign_id == campaign_id)
        .scalar()
    )
    now = datetime.utcnow()
    if max_scheduled is None or max_scheduled < now:
        slot = now + timedelta(minutes=5)
    else:
        slot = max_scheduled + timedelta(minutes=50)
    # Replacements land inside the 9-5 window too (audit PS-N16); the sender
    # also enforces it, this just keeps the dashboard's times honest.
    from services.email_campaign.campaign_worker import _campaign_tz, _push_to_business_hours
    campaign = db.get(Campaign, campaign_id)
    return _push_to_business_hours(slot, _campaign_tz(campaign)) if campaign else slot


def _log_to_order(
    db: Session,
    campaign: Campaign,
    msg: str,
) -> None:
    """Append a timestamped event to OutreachOrder.action_log if such an order
    exists. Best-effort: never raises."""
    try:
        from services.order_links import order_for_campaign
        order = order_for_campaign(db, campaign)
        if not order:
            return
        log = list(order.action_log or [])
        log.append({"ts": datetime.utcnow().isoformat(), "msg": msg})
        order.action_log = log
    except Exception as e:  # noqa: BLE001 — action_log is non-essential
        logger.warning("[REPLENISH] action_log append failed for campaign %d: %s",
                       campaign.id, e)


def add_replacement_lead(
    db: Session,
    campaign_id: int,
    replaced_email_id: int,
    reason: ReplacementReason,
) -> Optional[int]:
    """Promote a fresh lead in place of a bounce / no-match.

    Returns the new emails_sent.id on success, None if:
      - the campaign has hit its 25% replacement cap
      - the source row is itself a replacement (prevents infinite chains)
      - the source row already has a replacement
      - no candidate leads remain in the user's pool
    """
    # Locked, so two replicas handling the same bounce cannot both pass the
    # one-stand-in check below before either inserts (PP-P45).
    src = db.query(EmailSent).filter(EmailSent.id == replaced_email_id).with_for_update().first()
    if not src:
        return None
    if src.replacement_for_id is not None:
        return None  # don't replace a replacement
    # One stand-in per source row. Without this a bounce reported twice (or a
    # retry of the same no-match) queued a second free replacement: 256 sources
    # had more than one (audit PP-P45).
    if db.query(EmailSent.id).filter(EmailSent.replacement_for_id == src.id).first() is not None:
        return None

    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        return None

    # A no-match stand-in takes a paid slot; none once the campaign has used
    # every credit it paid for (PP-P26). Bounce stand-ins are free.
    if reason != "bounce" and campaign.credits_reserved is not None:
        from services.email_campaign.campaign_worker import _COMMITTED, _paid_slots_in
        allowed = max(0, campaign.credits_reserved - (campaign.credits_released or 0))
        if _paid_slots_in(db, campaign_id, _COMMITTED) >= allowed:
            logger.info("[REPLENISH] Campaign %d at its paid cap; no replacement for email %d",
                        campaign_id, replaced_email_id)
            return None

    initial_size = _campaign_initial_size(db, campaign_id)
    # floor, not ceil: ceil gave 50-credit plans 13 replacements, 26% (audit P45).
    cap = math.floor(initial_size * REPLACEMENT_CAP_PERCENT)
    used = _campaign_replacements_used(db, campaign_id)
    if used >= cap:
        logger.info(
            "[REPLENISH] Campaign %d at cap (%d/%d) — skipping %s replacement for email %d",
            campaign_id, used, cap, reason, replaced_email_id,
        )
        return None

    new_lead = _pick_next_lead(db, campaign)
    if not new_lead:
        logger.info(
            "[REPLENISH] Campaign %d has no remaining leads in pool — cannot replace email %d",
            campaign_id, replaced_email_id,
        )
        return None

    scheduled_at = _next_schedule_slot(db, campaign_id)

    new_email = EmailSent(
        campaign_id=campaign_id,
        lead_id=new_lead.id,
        status="pending_enrichment" if not new_lead.email else "queued",
        enrichment_status="pending" if not new_lead.email else "enriched",
        scheduled_at=scheduled_at,
        followup_number=0,
        replacement_for_id=replaced_email_id,
        replacement_reason=reason,
    )
    if new_lead.email and new_lead.email_verified:
        new_email.to_email = new_lead.email

    db.add(new_email)
    db.flush()  # populate new_email.id

    _log_to_order(
        db,
        campaign,
        f"Replacement (#{new_email.id}) queued for #{replaced_email_id} — reason: {reason}, "
        f"lead {new_lead.id} ({new_lead.name or '?'}@{new_lead.company or '?'})",
    )

    db.commit()
    logger.info(
        "[REPLENISH] Campaign %d: replaced email %d (reason=%s) with email %d, lead %d. Cap %d/%d.",
        campaign_id, replaced_email_id, reason, new_email.id, new_lead.id, used + 1, cap,
    )
    return new_email.id

