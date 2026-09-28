"""Hourly reconciliation of states nothing else ever repairs.

Runs from the launch-nudge sweep (hourly, one replica at a time).

  drafts   A campaign created but never launched (tab closed between
           create and send, audit P27) holds its credits forever. After an
           hour it is cancelled, which returns them.
  orders   An order at campaign_running whose campaign row is gone (deleted
           out of band; the FK nulled campaign_id, audit P44) sent the user
           to an empty dashboard. It goes back to campaign_setup.
  links    A campaign no order points at (audit P03: the pointer was
           overwritten by a second campaign) is linked through
           campaigns.outreach_order_id to the user's order that was current
           when it was created, so order views see it.
  wallets  Credits reserved by a user who owns no campaign and whose wallet
           has not moved in 24 hours are a reservation for something that no
           longer exists (P31/P44: 51 credits for one paying user). They are
           released, ledgered as release_admin.
"""

from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.logger import get_logger
from database.models import Campaign, Candidate, CreditLedger, OutreachOrder, UserCredit
from services import credits

logger = get_logger(__name__)

DRAFT_MAX_AGE = timedelta(hours=1)
WALLET_QUIET = timedelta(hours=24)
ACTOR = "reconcile"


def sweep_stale_drafts(db: Session, now: datetime) -> int:
    from services.email_campaign.campaign_worker import finish_campaign
    stale = (
        db.query(Campaign)
        .filter(Campaign.status == "draft", Campaign.created_at < now - DRAFT_MAX_AGE)
        .all()
    )
    for c in stale:
        finish_campaign(db, c, reason="created but never launched", final_status="cancelled")
    return len(stale)


def reset_orders_without_campaign(db: Session, now: datetime) -> int:
    live_order_ids = {
        oid for (oid,) in db.query(Campaign.outreach_order_id)
        .filter(Campaign.outreach_order_id.isnot(None))
    }
    orders = (
        db.query(OutreachOrder)
        .filter(OutreachOrder.status == "campaign_running", OutreachOrder.campaign_id.is_(None))
        .all()
    )
    n = 0
    for o in orders:
        if o.id in live_order_ids:
            continue
        o.status = "campaign_setup"
        log = list(o.action_log or [])
        log.append({"ts": now.isoformat(), "msg": f"Reset to campaign_setup: its campaign no longer exists ({ACTOR})"})
        o.action_log = log
        o.updated_at = now
        n += 1
    db.commit()
    return n


def link_orphan_campaigns(db: Session, now: datetime) -> int:
    orphans = db.query(Campaign).filter(Campaign.outreach_order_id.is_(None)).all()
    n = 0
    for c in orphans:
        owner = db.query(Candidate.user_id).filter(Candidate.id == c.candidate_id).scalar()
        if owner is None:
            continue
        pointing = db.query(OutreachOrder).filter(OutreachOrder.campaign_id == c.id).order_by(OutreachOrder.id.desc()).first()
        order = pointing or (
            db.query(OutreachOrder)
            .filter(OutreachOrder.user_id == owner, OutreachOrder.created_at <= (c.created_at or now))
            .order_by(OutreachOrder.created_at.desc())
            .first()
        )
        if order is not None:
            c.outreach_order_id = order.id
            n += 1
    db.commit()
    return n


def release_orphan_reservations(db: Session, now: datetime) -> int:
    owners = {uid for (uid,) in db.query(Candidate.user_id).join(Campaign, Campaign.candidate_id == Candidate.id).distinct()}
    released = 0
    for wallet in db.query(UserCredit).filter(UserCredit.used_credits > 0).all():
        if wallet.user_id in owners:
            continue
        last_move = (
            db.query(func.max(CreditLedger.created_at))
            .filter(CreditLedger.user_id == wallet.user_id, CreditLedger.reason != "opening_balance")
            .scalar()
        )
        if last_move is not None and now - last_move < WALLET_QUIET:
            continue  # something (enrichment, a send) is using it right now
        released += credits.release(db, wallet.user_id, wallet.used_credits, credits.RELEASE_ADMIN,
                                    actor=ACTOR, note="reservation held with no campaign")
    db.commit()
    return released


def run(db: Session, now: datetime = None) -> dict:
    now = now or datetime.utcnow()
    out = {}
    for name, fn in (("drafts_cancelled", sweep_stale_drafts),
                     ("campaigns_linked", link_orphan_campaigns),
                     ("orders_reset", reset_orders_without_campaign),
                     ("credits_released", release_orphan_reservations)):
        try:
            out[name] = fn(db, now)
        except Exception:
            db.rollback()
            logger.exception("[RECONCILE] %s failed", name)
    if any(out.values()):
        logger.info("[RECONCILE] %s", out)
    return out
