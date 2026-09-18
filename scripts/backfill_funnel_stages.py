"""Backfill funnel stage timestamps on existing outreach_orders.

The new per-stage timestamps (resume_uploaded_at, quiz_started_at, ...) were
added in migration 025 and are populated going forward by the stage_tracking
helper. This script reconstructs them for users who already moved through the
funnel before the instrumentation existed.

Sources, in priority order:
  1. OutreachOrder.action_log entries  — most accurate when present
  2. OutreachOrder.created_at + status — fallback floor when log is sparse
  3. Candidate.created_at              — resume_uploaded_at, quiz_completed_at
  4. PaymentOrder rows                 — payment_page_reached_at, payment_made_at
  5. EmailAccount.created_at           — gmail_connected_at
  6. Campaign timestamps               — campaign_setup_at, campaign_launched_at,
                                         campaign_paused_at, campaign_completed_at

The script is idempotent — it never overwrites a non-null timestamp, so it's
safe to run multiple times. It also creates missing OutreachOrder rows for
candidates that never reached lead discovery (so users who uploaded a resume
but bailed at the quiz still appear in the funnel).

Usage:
    python -m scripts.backfill_funnel_stages           # dry run, prints counts
    python -m scripts.backfill_funnel_stages --apply   # commit the changes
"""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict
from datetime import datetime
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from database.session import SessionLocal
from database.models import (
    Campaign,
    Candidate,
    EmailAccount,
    Lead,
    OutreachOrder,
    PaymentOrder,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# action_log message → stage column. Match by substring to tolerate variations.
LOG_STAGE_HINTS = [
    ("Stage: resume_uploaded",       "resume_uploaded_at"),
    ("Stage: quiz_started",          "quiz_started_at"),
    ("Stage: quiz_completed",        "quiz_completed_at"),
    ("Stage: leads_generated",       "leads_generated_at"),
    ("Stage: payment_page_reached",  "payment_page_reached_at"),
    ("Stage: payment_made",          "payment_made_at"),
    ("Stage: gmail_connected",       "gmail_connected_at"),
    ("Stage: email_style_selected",  "email_style_selected_at"),
    ("Stage: campaign_setup",        "campaign_setup_at"),
    ("Stage: campaign_launched",     "campaign_launched_at"),
    ("Stage: campaign_paused",       "campaign_paused_at"),
    ("Stage: campaign_completed",    "campaign_completed_at"),
    # Legacy entries from before stage_tracking existed.
    ("leads_ready",                  "leads_generated_at"),
    ("Auto-advanced to leads_ready", "leads_generated_at"),
    ("campaign_setup",               "campaign_setup_at"),
    ("Campaign created",             "campaign_setup_at"),
    ("running",                      "campaign_launched_at"),
    ("paused",                       "campaign_paused_at"),
    ("completed",                    "campaign_completed_at"),
]


# Minimum status reached → which stages are guaranteed to have happened.
# Used as a floor when we have no better timestamp.
STATUS_FLOORS = {
    "created":              ["resume_uploaded_at"],
    "profile_complete":     ["resume_uploaded_at", "quiz_completed_at"],
    "leads_generating":     ["resume_uploaded_at", "quiz_completed_at"],
    "leads_ready":          ["resume_uploaded_at", "quiz_completed_at", "leads_generated_at"],
    "enriching":            ["resume_uploaded_at", "quiz_completed_at", "leads_generated_at"],
    "enrichment_complete":  ["resume_uploaded_at", "quiz_completed_at", "leads_generated_at"],
    "campaign_setup":       ["resume_uploaded_at", "quiz_completed_at", "leads_generated_at",
                             "payment_made_at", "gmail_connected_at", "campaign_setup_at"],
    "email_connected":      ["resume_uploaded_at", "quiz_completed_at", "leads_generated_at",
                             "payment_made_at", "gmail_connected_at", "campaign_setup_at"],
    "campaign_running":     ["resume_uploaded_at", "quiz_completed_at", "leads_generated_at",
                             "payment_made_at", "gmail_connected_at", "campaign_setup_at",
                             "campaign_launched_at"],
    "completed":            ["resume_uploaded_at", "quiz_completed_at", "leads_generated_at",
                             "payment_made_at", "gmail_connected_at", "campaign_setup_at",
                             "campaign_launched_at", "campaign_completed_at"],
}


def _set_if_empty(order: OutreachOrder, column: str, ts: Optional[datetime]) -> bool:
    if ts is None:
        return False
    if getattr(order, column, None) is not None:
        return False
    setattr(order, column, ts)
    return True


def _parse_log_ts(entry: dict) -> Optional[datetime]:
    raw = entry.get("ts")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _earliest_log_ts(action_log: list, hint: str) -> Optional[datetime]:
    """Earliest timestamp in action_log whose message contains `hint`."""
    candidates = []
    for entry in action_log or []:
        msg = entry.get("msg", "")
        if hint in msg:
            ts = _parse_log_ts(entry)
            if ts is not None:
                candidates.append(ts)
    return min(candidates) if candidates else None


def backfill_existing_orders(db: Session, apply_changes: bool) -> dict:
    counts: dict = defaultdict(int)
    orders = db.query(OutreachOrder).all()
    logger.info("Scanning %d existing outreach_orders", len(orders))

    for order in orders:
        before = {col: getattr(order, col) for col in [
            "resume_uploaded_at", "quiz_started_at", "quiz_completed_at",
            "leads_generated_at", "payment_page_reached_at", "payment_made_at",
            "gmail_connected_at", "email_style_selected_at", "campaign_setup_at",
            "campaign_launched_at", "campaign_paused_at", "campaign_completed_at",
        ]}
        log = order.action_log or []

        # 1. Use action_log hints (highest accuracy).
        for hint, column in LOG_STAGE_HINTS:
            if getattr(order, column, None) is not None:
                continue
            ts = _earliest_log_ts(log, hint)
            if ts and _set_if_empty(order, column, ts):
                counts[f"log:{column}"] += 1

        # 2. Stage 1/3 — derive from ALL of the user's candidates (not just
        #    the one linked to this order). A user can have multiple Candidate
        #    rows from re-uploads; their stage signal is the union across all
        #    of them. Without this, an order linked to candidate_A misses
        #    quiz/leads signals living on candidate_B for the same user.
        user_candidates = db.query(Candidate).filter_by(user_id=order.user_id).all()
        user_candidate_ids = [c.id for c in user_candidates]

        if user_candidates:
            earliest_resume = min((c.created_at for c in user_candidates if c.created_at), default=None)
            if earliest_resume and _set_if_empty(order, "resume_uploaded_at", earliest_resume):
                counts["candidate:resume_uploaded_at"] += 1
            quiz_dates = [c.created_at for c in user_candidates if c.psychometric_profile and c.created_at]
            if quiz_dates and _set_if_empty(order, "quiz_completed_at", min(quiz_dates)):
                counts["candidate:quiz_completed_at"] += 1

        # 3. Stage 4 — leads_generated via ANY of the user's candidates.
        #    Previously this only checked the order's own candidate_id, which
        #    missed 192 users whose leads lived under a different candidate row.
        if user_candidate_ids and getattr(order, "leads_generated_at", None) is None:
            earliest_lead = (
                db.query(Lead.created_at)
                .filter(Lead.candidate_id.in_(user_candidate_ids))
                .order_by(Lead.created_at.asc())
                .first()
            )
            if earliest_lead and earliest_lead[0]:
                if _set_if_empty(order, "leads_generated_at", earliest_lead[0]):
                    counts["leads:leads_generated_at"] += 1

        # 4. Stages 5/6 — from PaymentOrder.
        po_rows = db.query(PaymentOrder).filter_by(user_id=order.user_id).all()
        for po in po_rows:
            if po.created_at and _set_if_empty(order, "payment_page_reached_at", po.created_at):
                counts["payment:payment_page_reached_at"] += 1
            if po.status == "paid":
                paid_ts = po.updated_at or po.created_at
                if paid_ts and _set_if_empty(order, "payment_made_at", paid_ts):
                    counts["payment:payment_made_at"] += 1

        # 5. Stage 7 — gmail_connected from EmailAccount.
        if order.email_account_id:
            ea = db.query(EmailAccount).filter_by(id=order.email_account_id).first()
            if ea and ea.created_at and _set_if_empty(order, "gmail_connected_at", ea.created_at):
                counts["email_account:gmail_connected_at"] += 1
        else:
            ea = (
                db.query(EmailAccount)
                .filter(EmailAccount.user_id == order.user_id)
                .order_by(EmailAccount.created_at.asc())
                .first()
            )
            if ea and ea.created_at and _set_if_empty(order, "gmail_connected_at", ea.created_at):
                counts["email_account:gmail_connected_at"] += 1

        # 6. Stages 9/10/11/12 — derive from ALL of the user's campaigns,
        #    not just the order's linked campaign_id. A user can have run
        #    multiple campaigns; the earliest setup / launched timestamp
        #    across all of them is the right signal.
        user_campaigns: list = []
        if user_candidate_ids:
            user_campaigns = (
                db.query(Campaign)
                .filter(Campaign.candidate_id.in_(user_candidate_ids))
                .all()
            )
        if user_campaigns:
            earliest_setup = min((c.created_at for c in user_campaigns if c.created_at), default=None)
            if earliest_setup and _set_if_empty(order, "campaign_setup_at", earliest_setup):
                counts["campaign:campaign_setup_at"] += 1
            style_dates = [c.created_at for c in user_campaigns if c.selected_styles and c.created_at]
            if style_dates and _set_if_empty(order, "email_style_selected_at", min(style_dates)):
                counts["campaign:email_style_selected_at"] += 1
            launch_dates = [
                c.started_at or c.created_at
                for c in user_campaigns
                if c.started_at or c.status in ("running", "paused", "completed")
            ]
            launch_dates = [d for d in launch_dates if d]
            if launch_dates and _set_if_empty(order, "campaign_launched_at", min(launch_dates)):
                counts["campaign:campaign_launched_at"] += 1
            pause_dates = [c.paused_at for c in user_campaigns if c.paused_at]
            if pause_dates and _set_if_empty(order, "campaign_paused_at", min(pause_dates)):
                counts["campaign:campaign_paused_at"] += 1
            complete_dates = [c.completed_at for c in user_campaigns if c.completed_at]
            if not complete_dates:
                # Status fallback for legacy campaigns whose completed_at was never recorded
                for c in user_campaigns:
                    if c.status == "completed":
                        complete_dates.append(c.completed_at or order.updated_at or c.created_at)
                complete_dates = [d for d in complete_dates if d]
            if complete_dates and _set_if_empty(order, "campaign_completed_at", min(complete_dates)):
                counts["campaign:campaign_completed_at"] += 1

        # 7. STATUS_FLOORS — for stages still null, use the order's own created_at
        #    as a last-resort floor based on what status proves they reached.
        guaranteed = STATUS_FLOORS.get(order.status, [])
        floor_ts = order.created_at
        if floor_ts:
            for column in guaranteed:
                if _set_if_empty(order, column, floor_ts):
                    counts[f"floor:{column}"] += 1

        after = {col: getattr(order, col) for col in before}
        changed = sum(1 for col, v in after.items() if before[col] is None and v is not None)
        if changed:
            counts["orders_touched"] += 1

    if apply_changes:
        db.commit()
        logger.info("COMMITTED — backfill applied")
    else:
        db.rollback()
        logger.info("DRY RUN — no changes saved (use --apply to commit)")

    return dict(counts)


def create_missing_orders_for_orphan_candidates(db: Session, apply_changes: bool) -> int:
    """For users who uploaded a resume (Candidate row exists) but have no
    OutreachOrder, create one so they appear in the funnel at stage 1."""
    sub_q = db.query(OutreachOrder.user_id).distinct().subquery()
    orphan_candidates = (
        db.query(Candidate)
        .filter(Candidate.user_id.notin_(sub_q.select()))
        .all()
    )
    logger.info("Found %d orphan candidates (resume uploaded, no OutreachOrder)",
                len(orphan_candidates))

    created = 0
    seen_users = set()
    for cand in orphan_candidates:
        # Avoid creating multiple orders for the same user across multiple
        # candidate rows — pick the earliest.
        if cand.user_id in seen_users:
            continue
        seen_users.add(cand.user_id)

        # Quiz completion implied if psychometric_profile is non-null.
        quiz_done_at = cand.created_at if cand.psychometric_profile else None

        order = OutreachOrder(
            user_id=cand.user_id,
            candidate_id=cand.id,
            status="created",
            action_log=[{"ts": (cand.created_at or datetime.utcnow()).isoformat(),
                         "msg": "Order backfilled from orphan candidate"}],
            resume_uploaded_at=cand.created_at,
            quiz_completed_at=quiz_done_at,
            created_at=cand.created_at or datetime.utcnow(),
            updated_at=cand.created_at or datetime.utcnow(),
        )
        db.add(order)
        created += 1

    if apply_changes:
        db.commit()
        logger.info("COMMITTED — created %d backfilled orders", created)
    else:
        db.rollback()
        logger.info("DRY RUN — would create %d backfilled orders", created)

    return created


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Commit changes (default: dry run)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        # Phase 1: backfill timestamps on existing orders.
        counts = backfill_existing_orders(db, apply_changes=args.apply)
        logger.info("Backfill counts:")
        for k in sorted(counts):
            logger.info("  %-44s %d", k, counts[k])

        # Phase 2: create orders for orphan candidates so the top-of-funnel
        # number reflects every resume upload.
        new_orders = create_missing_orders_for_orphan_candidates(db, apply_changes=args.apply)
        logger.info("Orphan-candidate orders created: %d", new_orders)

    finally:
        db.close()


if __name__ == "__main__":
    main()
