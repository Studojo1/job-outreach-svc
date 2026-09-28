"""One-off: the order-side leftovers of audit rows P12 and P10.

Dry run by default; --apply commits. Every order it touches gets an
action_log line naming this script.

 P12a  Paid payments with no outreach_order_id (30 payments, 24 users) are
       linked to the order that was the user's active one when they paid:
       their most recent order created at or before the payment, else their
       oldest. No order is created.
 P12b  A paying user's most recent order still at a pre-setup status is
       promoted to campaign_setup (same rule as the live safety net,
       services.stage_tracking.promote_paid_order).
 P10   Orders that recorded campaign_completed_at but whose campaign row no
       longer exists (deleted; the FK nulled campaign_id) move to
       'completed', so /orders/active stops returning a finished run.

Usage: python -m scripts.backfill_order_links [--apply]
"""

import argparse
import sys
from datetime import datetime

from database.models import OutreachOrder, PaymentOrder
from database.session import SessionLocal
from services.stage_tracking import promote_paid_order

ACTOR = "backfill-order-links"


def _log(order: OutreachOrder, msg: str, now: datetime) -> None:
    log = list(order.action_log or [])
    log.append({"ts": now.isoformat(), "msg": f"{msg} ({ACTOR})"})
    order.action_log = log
    order.updated_at = now


def run(db, apply: bool) -> dict:
    now = datetime.utcnow()
    out = {"payments_linked": 0, "orders_promoted": 0, "orders_completed": 0}

    paid = (
        db.query(PaymentOrder)
        .filter(PaymentOrder.status.in_(("paid", "completed")), PaymentOrder.amount_cents > 0)
        .all()
    )
    for p in paid:
        if p.outreach_order_id:
            continue
        orders = (
            db.query(OutreachOrder).filter(OutreachOrder.user_id == p.user_id)
            .order_by(OutreachOrder.created_at.asc()).all()
        )
        if not orders:
            continue
        before = [o for o in orders if o.created_at and p.created_at and o.created_at <= p.created_at]
        target = before[-1] if before else orders[0]
        p.outreach_order_id = target.id
        _log(target, f"Linked unlinked payment {p.id}", now)
        out["payments_linked"] += 1

    for user_id in sorted({p.user_id for p in paid}):
        latest = (
            db.query(OutreachOrder).filter(OutreachOrder.user_id == user_id)
            .order_by(OutreachOrder.created_at.desc()).first()
        )
        if latest is not None and promote_paid_order(latest, ACTOR):
            out["orders_promoted"] += 1

    stale = (
        db.query(OutreachOrder)
        .filter(OutreachOrder.campaign_completed_at.isnot(None),
                OutreachOrder.campaign_id.is_(None),
                OutreachOrder.status != "completed")
        .all()
    )
    for o in stale:
        o.status = "completed"
        _log(o, "Completed: its campaign finished and no longer exists", now)
        out["orders_completed"] += 1

    if apply:
        db.commit()
    else:
        db.rollback()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    db = SessionLocal()
    try:
        out = run(db, apply=args.apply)
    finally:
        db.close()
    print(("APPLIED: " if args.apply else "DRY RUN (rolled back): ") +
          ", ".join(f"{k}={v}" for k, v in out.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
