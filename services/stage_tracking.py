"""Stage tracking — funnel timestamp helpers for OutreachOrder.

Every user touching the outreach product passes through up to 13 stages.
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


# Ordered list of all 13 funnel stages (used by the admin endpoint).
STAGES = [
    "resume_uploaded",
    "quiz_started",
    "quiz_completed",
    "leads_generated",
    "leads_viewed",          # results page actually rendered leads (frontend ping)
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
            # Once an order has generated leads, its candidate_id is frozen.
            #
            # Re-pointing it unconditionally is how 88 orders ended up owning a
            # candidate row that holds no leads while the same user's leads sat
            # on a sibling row — 5 of those users had paid. The sequence: user
            # uploads a resume (candidate A), generates leads against A, then
            # uploads again (candidate B). This method moved the order to B, and
            # every downstream reader that goes order -> candidate -> leads
            # found nothing, because the leads are still on A.
            #
            # Before leads exist, re-pointing is correct and expected: a user
            # who re-uploads before generating should have the new resume used.
            # An order with no candidate yet has nothing to strand, so it stays
            # linkable: stage 1 can create the order before the candidate row
            # exists, and that link must still be able to land.
            if order.candidate_id is not None and order.leads_generated_at is not None:
                logger.info(
                    "[STAGE] order=%s keeps candidate=%s (leads already "
                    "generated); refusing to re-point at candidate=%s",
                    order.id, order.candidate_id, candidate_id,
                )
            else:
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


# Statuses an order holds before its leads exist. Discovery only ever moves an
# order forward out of one of these; an order already past them is left alone.
_PRE_LEADS_STATUSES = ("created", "profile_complete", "leads_generating")


def advance_discovery_status(
    db: Session,
    user_id: str,
    candidate_id: int,
    status: str,
) -> Optional[OutreachOrder]:
    """Move the user's active order to `leads_generating` or `leads_ready`.

    Called when discovery starts and when its leads are stored. Without it
    nothing ever wrote either status: the order created at resume upload sat at
    'created', so the later leads_ready -> campaign_setup update was rejected
    and My Orders showed 'Created' for a user holding a full lead set.

    Forward only: an order already past discovery (paid, campaign running) is
    never moved back. An order pinned to a different candidate's leads is not
    this run's order, so it is left alone too.
    """
    if status not in ("leads_generating", "leads_ready"):
        raise ValueError(f"not a discovery status: {status}")

    order = get_or_create_active_order(db, user_id, candidate_id=candidate_id)
    if not order.candidate_id:
        order.candidate_id = candidate_id
    if order.candidate_id == candidate_id and order.status in _PRE_LEADS_STATUSES \
            and order.status != status:
        prev = order.status
        order.status = status
        order.updated_at = datetime.utcnow()
        log = list(order.action_log or [])
        log.append({"ts": datetime.utcnow().isoformat(), "msg": f"Status: {prev} → {status} (discovery)"})
        order.action_log = log
        logger.info("[STAGE] order=%s user=%s status %s -> %s", order.id, user_id, prev, status)
    db.commit()
    return order


def safe_advance_discovery_status(db: Session, user_id: str, candidate_id: int, status: str) -> None:
    """Fire-and-forget advance_discovery_status: order bookkeeping must never
    fail a discovery run."""
    try:
        advance_discovery_status(db, user_id, candidate_id, status)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("[STAGE] advance_discovery_status failed (swallowed) status=%s user=%s", status, user_id)


# Every status before campaign_setup. A user who has paid must never be left
# at one of these.
FROZEN_BEHIND_PAYMENT = ("created", "leads_generating", "leads_ready", "enriching", "enrichment_complete")


def promote_paid_order(order: Optional[OutreachOrder], reason: str) -> bool:
    """Safety net: move an order a paid-for user owns out of a pre-payment status.

    The frontend normally advances order.status after payment, but if that call
    is missed or rejected the user is stuck at an early stage and the app
    re-shows "pay" (support ticket #19: paid + credited but order frozen at
    'created'). Promote any early-stage order to campaign_setup so they can
    build their campaign. Returns True when the order moved. Does not commit.
    """
    if order is None or order.status not in FROZEN_BEHIND_PAYMENT:
        return False
    prev = order.status
    order.status = "campaign_setup"
    log = list(order.action_log or [])
    log.append({
        "ts": datetime.utcnow().isoformat(),
        "msg": f"Auto-advanced {prev} -> campaign_setup on payment ({reason})",
    })
    order.action_log = log
    order.updated_at = datetime.utcnow()
    logger.info("[STAGE] Advanced outreach_order %s (%s -> campaign_setup): %s", order.id, prev, reason)
    return True


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
