"""Re-point orders whose candidate row holds none of the leads they generated.

The bug that produced these is fixed (stage_tracking.get_or_create_active_order
now refuses to re-point an order once leads_generated_at is set), but the rows
it already damaged are still damaged. Production at the time of writing: 88
orders whose candidate has zero leads while a sibling candidate of the SAME
user does have them. Five of those users paid.

How they got that way:

    upload resume          -> candidate A, order.candidate_id = A
    generate leads         -> leads written against A, leads_generated_at set
    upload another resume  -> candidate B, order.candidate_id = B   <-- damage

Everything downstream walks order -> candidate -> leads, so the user opens a
dashboard built from candidate B and sees nothing, while the leads they paid
for sit on candidate A.

Choosing the right candidate is the whole problem, and it is why this is a
reviewed script rather than one UPDATE. 928 users have more than one candidate
row; the worst has 190. So a user's "other candidate with leads" is not always
unique. The rule here:

    among the user's OTHER candidates that have leads, pick the one whose
    most recent lead was created at or before the order's leads_generated_at,
    closest to it in time — that is the batch this order paid for.

Any order where that rule does not land on exactly one candidate is reported as
AMBIGUOUS and left alone for a human. Silently guessing on a paid order is
worse than leaving it for review.

Usage:
    python -m scripts.reconcile_stranded_orders                  # dry run
    python -m scripts.reconcile_stranded_orders --paid-only      # dry run, paid only
    python -m scripts.reconcile_stranded_orders --paid-only --apply
    python -m scripts.reconcile_stranded_orders --apply          # the rest

Run it with --paid-only first, check those five by hand, confirm the dashboards
populate, and only then run the remainder.
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from database.models import Candidate, Lead, OutreachOrder

# database.session is imported inside main() rather than here: importing it
# validates the full app config (DB URL, API keys), which a test that only
# exercises the candidate-selection rule has no business requiring.

logger = logging.getLogger("reconcile_stranded_orders")


def _lead_count(db: Session, candidate_id: int) -> int:
    return db.query(func.count(Lead.id)).filter(Lead.candidate_id == candidate_id).scalar() or 0


def _last_lead_at(db: Session, candidate_id: int):
    return db.query(func.max(Lead.created_at)).filter(Lead.candidate_id == candidate_id).scalar()


def _pick_candidate(db: Session, order: OutreachOrder) -> tuple[Optional[int], str]:
    """Return (candidate_id, reason). candidate_id is None when unsafe to guess."""
    siblings = (
        db.query(Candidate)
        .filter(Candidate.user_id == order.user_id, Candidate.id != order.candidate_id)
        .all()
    )

    with_leads = []
    for c in siblings:
        n = _lead_count(db, c.id)
        if n:
            with_leads.append((c, n, _last_lead_at(db, c.id)))

    if not with_leads:
        return None, "no sibling candidate has leads"

    if len(with_leads) == 1:
        c, n, _ = with_leads[0]
        return c.id, f"only sibling with leads ({n} leads)"

    # Several siblings have leads. Prefer the batch this order actually
    # generated: the newest one that is not newer than leads_generated_at.
    cutoff = order.leads_generated_at
    if cutoff is None:
        return None, f"{len(with_leads)} siblings have leads and order has no leads_generated_at"

    eligible = [t for t in with_leads if t[2] is not None and t[2] <= cutoff]
    if not eligible:
        return None, f"{len(with_leads)} siblings have leads, none at or before leads_generated_at"

    eligible.sort(key=lambda t: t[2], reverse=True)
    best, n, last_at = eligible[0]

    # If two candidates' lead batches are within a minute of each other there is
    # no honest way to tell them apart. Leave it.
    if len(eligible) > 1:
        runner_up_at = eligible[1][2]
        if abs((last_at - runner_up_at).total_seconds()) < 60:
            return None, f"{len(eligible)} sibling lead batches within 60s of each other"

    return best.id, f"closest lead batch at or before leads_generated_at ({n} leads, last {last_at})"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="commit the re-points (default: dry run)")
    ap.add_argument("--paid-only", action="store_true", help="only orders with payment_made_at set")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from database.session import SessionLocal

    db: Session = SessionLocal()

    try:
        q = db.query(OutreachOrder).filter(
            OutreachOrder.candidate_id.isnot(None),
            OutreachOrder.leads_generated_at.isnot(None),
        )
        if args.paid_only:
            q = q.filter(OutreachOrder.payment_made_at.isnot(None))

        stranded, fixable, ambiguous = 0, [], []

        for order in q.all():
            # Stranded means: the order's own candidate has no leads at all.
            if _lead_count(db, order.candidate_id) > 0:
                continue
            stranded += 1

            target, reason = _pick_candidate(db, order)
            row = (order, target, reason)
            (fixable if target else ambiguous).append(row)

        paid_note = " (paid only)" if args.paid_only else ""
        logger.info("Stranded orders%s: %d", paid_note, stranded)
        logger.info("  re-pointable: %d", len(fixable))
        logger.info("  ambiguous, left for review: %d", len(ambiguous))
        logger.info("")

        for order, target, reason in fixable:
            paid = "PAID " if order.payment_made_at else ""
            logger.info(
                "%sorder=%s user=%s  candidate %s -> %s   [%s]",
                paid, order.id, order.user_id, order.candidate_id, target, reason,
            )

        if ambiguous:
            logger.info("")
            logger.info("AMBIGUOUS — not touched:")
            for order, _, reason in ambiguous:
                paid = "PAID " if order.payment_made_at else ""
                logger.info(
                    "%sorder=%s user=%s candidate=%s   [%s]",
                    paid, order.id, order.user_id, order.candidate_id, reason,
                )

        if not args.apply:
            logger.info("")
            logger.info("Dry run. Nothing written. Re-run with --apply to commit.")
            return

        for order, target, reason in fixable:
            order.candidate_id = target
            log = list(order.action_log or [])
            log.append({
                "ts": datetime.utcnow().isoformat(),
                "msg": f"Reconciled: candidate re-pointed to {target} ({reason})",
            })
            order.action_log = log

        db.commit()
        logger.info("")
        logger.info("Applied %d re-points. %d left for review.", len(fixable), len(ambiguous))

    finally:
        db.close()


if __name__ == "__main__":
    main()
