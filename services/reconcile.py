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
  grants   A real-money payment marked paid that never granted its credits
           (audit P16: one $27 order sat like that from June, its user with no
           wallet at all). After 5 minutes the grant it should have made is
           made, through the normal _finalize_credits, and the founders are told.
  leads    An unpaid order still at created/profile_complete whose leads
           exist moves to leads_ready (OP-N10).
  coupons  coupons.uses is raised to the payments each code paid for (PP-P14).
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


UNGRANTED_AFTER = timedelta(minutes=5)


def grant_paid_without_credits(db: Session, now: datetime) -> int:
    from database.models import PaymentOrder
    from api.routes_payment import _finalize_credits
    orders = (
        db.query(PaymentOrder)
        .filter(PaymentOrder.status.in_(("paid", "completed")),
                PaymentOrder.amount_cents > 0,
                (PaymentOrder.credits_granted.is_(None)) | (PaymentOrder.credits_granted == 0),
                PaymentOrder.created_at < now - UNGRANTED_AFTER)
        .all()
    )
    fixed = []
    for order in orders:
        _finalize_credits(db, order)
        if order.credits_granted:
            order.updated_at = now
            fixed.append(f"- payment {order.id} ({order.provider}, {order.amount_cents} {order.currency}) "
                         f"for user {order.user_id}: granted {order.credits_granted} credits it never got")
    db.commit()
    if fixed:
        _tell_founders("paid but never credited", "\n".join(fixed) + "\n\nThey now have the credits. "
                       "Refund instead via POST /admin/outreach/payments/{id}/refund if they prefer.")
    return len(fixed)


def _tell_founders(subject: str, message: str) -> None:
    from core.config import settings
    from services.launch_nudge import _env_tag, _send_template
    for to in [a.strip() for a in settings.OPS_ALERT_RECIPIENTS.split(",") if a.strip()]:
        _send_template({"to": to, "template": "ops-alert", "subject": f"{_env_tag()}{subject}", "message": message})


ENRICHMENT_JOB_DEAD_AFTER = timedelta(hours=1)


def release_dead_enrichment_jobs(db: Session, now: datetime) -> int:
    """A job still 'processing' after an hour died with its process (restart or
    deploy mid-run, audit P31). Release whatever it reserved and did not use."""
    from database.models import EnrichmentJob
    jobs = (db.query(EnrichmentJob)
            .filter(EnrichmentJob.status == "processing",
                    EnrichmentJob.updated_at < now - ENRICHMENT_JOB_DEAD_AFTER).all())
    total = 0
    for j in jobs:
        owed = max(0, (j.reserved or 0) - (j.enriched or 0) - (j.released or 0))
        if owed:
            total += credits.release(db, j.user_id, owed, credits.RELEASE_ENRICHMENT_UNUSED,
                                     actor=ACTOR, note=f"enrichment job {j.id} died mid-run")
            j.released = (j.released or 0) + owed
        j.status = "failed"
        j.error = "Stopped mid-run (process restarted); unused credits returned"
    db.commit()
    return total


LEDGER_ALERT_EVERY = timedelta(hours=24)


def check_ledger(db: Session, now: datetime) -> int:
    """Every wallet must equal the sum of its ledger rows (audit P29). A
    mismatch means something changed user_credits without services/credits.py.
    Reported to the founders at most daily; never auto-corrected, because the
    ledger cannot tell which side is right."""
    from sqlalchemy import text
    from database.models import SystemEvent
    rows = db.execute(text("""
        SELECT w.user_id, w.total_credits, w.used_credits,
               COALESCE(SUM(l.delta_total), 0) AS lt, COALESCE(SUM(l.delta_used), 0) AS lu
        FROM user_credits w LEFT JOIN credit_ledger l ON l.user_id = w.user_id
        GROUP BY w.user_id, w.total_credits, w.used_credits
        HAVING w.total_credits <> COALESCE(SUM(l.delta_total), 0)
            OR w.used_credits <> COALESCE(SUM(l.delta_used), 0)
    """)).fetchall()
    if not rows:
        return 0
    last = (db.query(func.max(SystemEvent.created_at))
            .filter(SystemEvent.event_type == "ledger_drift_alert").scalar())
    if last is None or now - last >= LEDGER_ALERT_EVERY:
        lines = [f"- {r.user_id}: wallet {r.total_credits}/{r.used_credits}, ledger {r.lt}/{r.lu}" for r in rows[:50]]
        _tell_founders(f"{len(rows)} wallet(s) out of step with the credit ledger",
                       "total/used per wallet vs ledger sums:\n" + "\n".join(lines))
        db.add(SystemEvent(event_type="ledger_drift_alert", created_at=now, meta={"wallets": len(rows)}))
        db.commit()
    return len(rows)


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


def advance_orders_with_leads(db: Session, now: datetime) -> int:
    """An unpaid order whose leads exist is at leads_ready (audit OP-N10).

    Discovery moves the order forward itself (stage_tracking.advance_
    discovery_status), but that step is fire-and-forget and skips an order
    pinned to another resume, so 2,737 orders sat at 'created' holding leads:
    My Orders said 'Created' and the admin status counts were wrong. Paid
    orders are left to promote_paid_order.
    """
    orders = (
        db.query(OutreachOrder)
        .filter(OutreachOrder.status.in_(("created", "profile_complete")),
                OutreachOrder.leads_generated_at.isnot(None),
                OutreachOrder.payment_made_at.is_(None))
        .all()
    )
    for o in orders:
        prev = o.status
        o.status = "leads_ready"
        log = list(o.action_log or [])
        log.append({"ts": now.isoformat(), "msg": f"Status: {prev} → leads_ready (leads exist; {ACTOR})"})
        o.action_log = log
        o.updated_at = now
    db.commit()
    return len(orders)


def sync_coupon_counts(db: Session, now: datetime) -> int:
    """coupons.uses never below the payments each code paid for (PP-P14)."""
    from database.models import Coupon, PaymentOrder
    from api.routes_payment import REDEEMED_STATUSES
    counts = dict(
        db.query(PaymentOrder.coupon_id, func.count(PaymentOrder.id))
        .filter(PaymentOrder.coupon_id.isnot(None), PaymentOrder.status.in_(REDEEMED_STATUSES))
        .group_by(PaymentOrder.coupon_id).all()
    )
    fixed = 0
    for c in db.query(Coupon).filter(Coupon.id.in_(list(counts))).all():
        if (c.uses or 0) < counts[c.id]:
            logger.warning("[RECONCILE] coupon %s uses %s < %s paid redemptions; corrected", c.id, c.uses, counts[c.id])
            c.uses = counts[c.id]
            fixed += 1
    db.commit()
    return fixed


def run(db: Session, now: datetime = None) -> dict:
    now = now or datetime.utcnow()
    out = {}
    for name, fn in (("drafts_cancelled", sweep_stale_drafts),
                     ("payments_credited", grant_paid_without_credits),
                     ("campaigns_linked", link_orphan_campaigns),
                     ("ledger_drift", check_ledger),
                     ("enrichment_credits_released", release_dead_enrichment_jobs),
                     ("orders_reset", reset_orders_without_campaign),
                     ("orders_advanced_to_leads_ready", advance_orders_with_leads),
                     ("coupon_counts_corrected", sync_coupon_counts),
                     ("credits_released", release_orphan_reservations)):
        try:
            out[name] = fn(db, now)
        except Exception:
            db.rollback()
            logger.exception("[RECONCILE] %s failed", name)
    if any(out.values()):
        logger.info("[RECONCILE] %s", out)
    return out
