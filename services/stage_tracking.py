"""Stage tracking — funnel timestamp helpers for OutreachOrder.

Every user touching the outreach product passes through up to 12 stages.
We record a timestamp on the user's OutreachOrder the first time they reach
each stage, which lets the admin dashboard compute drop-off across the
*entire* journey rather than only seeing the user's current state.

A single OutreachOrder is created at stage 1 (resume upload) so users who
abandon mid-funnel still leave a row behind. Stages 1–3 happen before any
candidate-specific work; later stages associate the order with a candidate /
campaign / email account as those resources come into existence.
"""

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from database.models import OutreachOrder

logger = logging.getLogger(__name__)


# Ordered list of all 12 funnel stages (used by the admin endpoint).
STAGES = [
    "resume_uploaded",
    "quiz_started",
    "quiz_completed",
    "leads_generated",
    "payment_page_reached",
    "payment_made",
    "gmail_connected",
    "email_style_selected",
    "campaign_setup",
    "campaign_launched",
    "campaign_paused",
    "campaign_completed",
]

_STAGE_COLUMN = {s: f"{s}_at" for s in STAGES}


def get_or_create_active_order(
    db: Session,
    user_id: str,
    candidate_id: Optional[int] = None,
    campaign_id: Optional[int] = None,
) -> OutreachOrder:
    """Return the order to record this stage against, creating one if needed.

    Resolution order:
      1. If campaign_id is given, the order that already owns that campaign.
         This is authoritative for campaign-stage events (launch/pause/complete)
         so the timestamp lands on the *right* order even when the user has
         multiple orders (e.g. an abandoned newer order created after launch).
      2. Otherwise the user's most recent non-completed order.
      3. Otherwise a new order.

    Used by stage 1 (resume upload) and any other entry point that may run
    before an order exists. Idempotent — never creates duplicate active orders.
    """
    order = None

    # 1. Campaign-stage events: bind to the order that owns this campaign.
    if campaign_id is not None:
        order = (
            db.query(OutreachOrder)
            .filter(
                OutreachOrder.user_id == user_id,
                OutreachOrder.campaign_id == campaign_id,
            )
            .order_by(OutreachOrder.created_at.asc())
            .first()
        )

    # 2. Fall back to the most recent non-completed order.
    if order is None:
        order = (
            db.query(OutreachOrder)
            .filter(
                OutreachOrder.user_id == user_id,
                OutreachOrder.status != "completed",
            )
            .order_by(OutreachOrder.created_at.desc())
            .first()
        )

    if order:
        if candidate_id and order.candidate_id != candidate_id:
            order.candidate_id = candidate_id
            order.updated_at = datetime.utcnow()
        return order

    order = OutreachOrder(
        user_id=user_id,
        candidate_id=candidate_id,
        status="created",
        action_log=[{"ts": datetime.utcnow().isoformat(), "msg": "Order created (auto, on funnel entry)"}],
    )
    db.add(order)
    db.flush()
    return order


def mark_stage(
    db: Session,
    user_id: str,
    stage: str,
    *,
    candidate_id: Optional[int] = None,
    campaign_id: Optional[int] = None,
    email_account_id: Optional[int] = None,
    commit: bool = True,
) -> Optional[OutreachOrder]:
    """Record the first time a user reached `stage` on their active order.

    - Creates an OutreachOrder if none exists (stage-1 entry point).
    - Sets the matching `<stage>_at` column to now() if not already set.
      This is one-shot: re-firing for the same stage is a no-op so we keep
      the *first* time each user reached that stage in the funnel.
    - For later stages, opportunistically links candidate_id / campaign_id /
      email_account_id onto the order so admin queries can stitch resources
      back to the funnel row.
    """
    if stage not in _STAGE_COLUMN:
        logger.warning("[STAGE] Unknown stage=%s, ignoring", stage)
        return None

    order = get_or_create_active_order(
        db, user_id, candidate_id=candidate_id, campaign_id=campaign_id
    )

    if candidate_id and not order.candidate_id:
        order.candidate_id = candidate_id
    if campaign_id and not order.campaign_id:
        order.campaign_id = campaign_id
    if email_account_id and not order.email_account_id:
        order.email_account_id = email_account_id

    column = _STAGE_COLUMN[stage]
    if getattr(order, column, None) is None:
        setattr(order, column, datetime.utcnow())
        order.updated_at = datetime.utcnow()
        log = list(order.action_log or [])
        log.append({"ts": datetime.utcnow().isoformat(), "msg": f"Stage: {stage}"})
        order.action_log = log
        logger.info("[STAGE] order=%s user=%s stage=%s", order.id, user_id, stage)

    if commit:
        try:
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("[STAGE] commit failed for stage=%s", stage)
            raise
    return order


def safe_mark_stage(db: Session, user_id: str, stage: str, **kwargs) -> None:
    """Fire-and-forget version. Swallows any exception so instrumentation
    can never break a user-facing flow. Use this from inside request
    handlers where the primary operation has already succeeded."""
    try:
        mark_stage(db, user_id, stage, **kwargs)
    except Exception:
        logger.exception("[STAGE] safe_mark_stage failed (swallowed) stage=%s user=%s", stage, user_id)
