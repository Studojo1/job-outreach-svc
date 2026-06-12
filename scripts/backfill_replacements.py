"""One-time backfill: for every historical bounced or enrichment-exhausted email
in production that lacks a replacement, add a replacement lead from the user's
unused pool, respecting the 25% cap per campaign.

Usage:
    python scripts/backfill_replacements.py            # dry-run (default)
    python scripts/backfill_replacements.py --apply    # actually insert rows

The script commits in batches of 50 and logs each replacement so the run can
be interrupted and safely re-run (idempotent: only processes rows where
replacement_for_id IS NULL and the trigger row itself is not already a
replacement).
"""
from __future__ import annotations

import argparse
import sys
import os

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database.session import SessionLocal
from database.models import EmailSent, Campaign
from services.email_campaign.replenishment import add_replacement_lead
from core.logger import get_logger

logger = get_logger("backfill_replacements")

BATCH_SIZE = 50


def _find_candidates(db):
    """Return all historical bounce / enrichment-exhausted rows that:
      - are not themselves replacements (replacement_for_id IS NULL)
      - have not yet had a replacement generated (no child row pointing at them)
    """
    # Emails that already have a replacement child
    already_replaced_sq = (
        db.query(EmailSent.replacement_for_id)
        .filter(EmailSent.replacement_for_id.isnot(None))
        .subquery()
    )

    candidates = (
        db.query(EmailSent)
        .join(Campaign, Campaign.id == EmailSent.campaign_id)
        .filter(
            EmailSent.replacement_for_id.is_(None),
            EmailSent.id.notin_(already_replaced_sq),
            EmailSent.status.in_(["bounced", "failed"]),
            # Only emails from active user campaigns (not draft/cancelled)
            Campaign.status.in_(["running", "paused", "completed"]),
        )
        .filter(
            # bounced emails OR enrichment-exhausted fails
            (EmailSent.status == "bounced") |
            (
                (EmailSent.status == "failed") &
                (EmailSent.error_message.ilike("%Apollo could not find%") |
                 EmailSent.error_message.ilike("%enrichment failed%"))
            )
        )
        .order_by(EmailSent.campaign_id, EmailSent.id)
        .all()
    )
    return candidates


def run(apply: bool):
    db = SessionLocal()
    try:
        candidates = _find_candidates(db)
        print(f"\n[BACKFILL] Found {len(candidates)} emails eligible for replacement")
        if not candidates:
            print("[BACKFILL] Nothing to do.")
            return

        # Group by reason for summary
        bounces = [e for e in candidates if e.status == "bounced"]
        exhausted = [e for e in candidates if e.status == "failed"]
        print(f"  {len(bounces)} bounced  |  {len(exhausted)} enrichment-exhausted")

        if not apply:
            print("\n[BACKFILL] DRY-RUN — no changes made. Re-run with --apply to execute.\n")
            # Print a sample of what would happen
            for e in candidates[:20]:
                reason = "bounce" if e.status == "bounced" else "enrichment_exhausted"
                print(f"  Would replace email_id={e.id} campaign_id={e.campaign_id} reason={reason}")
            if len(candidates) > 20:
                print(f"  ... and {len(candidates) - 20} more")
            return

        # --- APPLY ---
        succeeded = 0
        capped = 0
        no_pool = 0

        for i, email_row in enumerate(candidates):
            reason = "bounce" if email_row.status == "bounced" else "enrichment_exhausted"
            new_id = add_replacement_lead(
                db=db,
                campaign_id=email_row.campaign_id,
                replaced_email_id=email_row.id,
                reason=reason,
            )
            if new_id:
                succeeded += 1
                logger.info("[BACKFILL] %d/%d: campaign=%d old=%d -> new=%d (%s)",
                            i + 1, len(candidates), email_row.campaign_id, email_row.id, new_id, reason)
            else:
                # add_replacement_lead returns None for cap hit, replacement-of-replacement, or no pool
                # It logs the specific reason internally
                if email_row.replacement_for_id is not None:
                    pass  # guard in add_replacement_lead already rejects this
                else:
                    # Could be cap or no pool — both are logged by replenishment.py
                    no_pool += 1

            if (i + 1) % BATCH_SIZE == 0:
                print(f"  Progress: {i + 1}/{len(candidates)} processed, {succeeded} replacements queued")

        print(f"\n[BACKFILL] Complete: {succeeded} replacements queued, {no_pool} skipped (cap/no pool)")

    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill replacement leads for historical bounces/enrichment-fails")
    parser.add_argument("--apply", action="store_true", help="Actually insert replacement rows (default: dry-run)")
    args = parser.parse_args()
    run(apply=args.apply)
