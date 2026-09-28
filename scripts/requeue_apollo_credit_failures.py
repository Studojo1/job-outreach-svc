"""One-off: put back emails that failed only because Apollo was out of credits.

Two kinds of row died of an empty Apollo account instead of pausing:
  - "Enrichment error: All Apollo API keys are exhausted..." (the ValueError
    enrichment did not recognise; fixed in #110);
  - "Apollo could not find email for this contact" during the 2026-09-27/28
    outage, when Apollo answered 200 with no email. The normal rate is 0-5 a
    day; those two days had 84 and 63, and re-asking Apollo for sampled leads
    afterwards returned verified emails.

For each such paid email in a campaign that is still running or paused:
  - it goes back to pending enrichment and its lead's failure count resets;
  - an unsent replacement lead queued in its place is deleted (it was never
    sent, and keeping it would hand the user an extra email and use up the
    campaign's 25% replacement cap);
  - if it was not replaced, its credit had been returned (by outcomes.fail or
    by backfill-wave2), so one credit is reserved again. If the user no
    longer has it, the email is left alone and reported.
Running campaigns are then re-planned from now (compute_campaign_schedule,
resume=True), so the backlog cannot go out in one burst.

Completed/cancelled campaigns are not reopened: those credits are already
back with the user.

Dry run by default; every credit change is a ledger row with actor=ACTOR.
    python -m scripts.requeue_apollo_credit_failures            # dry run
    python -m scripts.requeue_apollo_credit_failures --apply
"""

import argparse
from collections import defaultdict
from datetime import datetime

from sqlalchemy import and_, or_

from database.models import Campaign, Candidate, EmailSent, Lead
from database.session import SessionLocal
from services import credits
from services.email_campaign.outcomes import holds_paid_slot

ACTOR = "requeue-apollo-credit"
OUTAGE_START = datetime(2026, 9, 27)
UNSENT = ("pending_enrichment", "queued")


def _targets(db):
    return (
        db.query(EmailSent)
        .join(Campaign, Campaign.id == EmailSent.campaign_id)
        .filter(
            Campaign.status.in_(("running", "paused")),
            EmailSent.status == "failed",
            EmailSent.sent_at.is_(None),
            or_(
                EmailSent.error_message.like("Enrichment error: All Apollo API keys are exhausted%"),
                and_(
                    EmailSent.error_message == "Apollo could not find email for this contact",
                    EmailSent.scheduled_at >= OUTAGE_START,
                ),
            ),
        )
        .order_by(EmailSent.campaign_id, EmailSent.id)
        .all()
    )


def run(db, apply: bool) -> dict:
    now = datetime.utcnow()
    report = defaultdict(lambda: defaultdict(int))
    touched_running = set()

    for email in _targets(db):
        campaign = db.get(Campaign, email.campaign_id)
        r = report[campaign.id]
        replacements = db.query(EmailSent).filter(EmailSent.replacement_for_id == email.id).all()
        if any(x.status not in UNSENT for x in replacements):
            # The slot was already used by a replacement that went out.
            r["skipped_replacement_sent"] += 1
            continue

        if not replacements and holds_paid_slot(email) and campaign.credits_reserved is not None:
            owner = db.query(Candidate.user_id).filter(Candidate.id == campaign.candidate_id).scalar()
            if credits.reserve(db, owner, 1, credits.RESERVE_CAMPAIGN, campaign=campaign,
                               actor=ACTOR, note=f"email {email.id}: requeued after Apollo credit outage") is None:
                r["skipped_no_credit"] += 1
                continue
            r["credits_retaken"] += 1

        for x in replacements:
            db.delete(x)
        r["replacements_deleted"] += len(replacements)

        email.status = "pending_enrichment"
        email.enrichment_status = "pending"
        email.status_changed_at = now
        email.error_message = None
        lead = db.get(Lead, email.lead_id) if email.lead_id else None
        if lead is not None:
            lead.enrichment_fail_count = 0
        r["requeued"] += 1
        r["status_" + campaign.status] = 1
        if campaign.status == "running":
            touched_running.add(campaign.id)

    if not apply:
        db.rollback()
        return report
    db.commit()

    from services.email_campaign.campaign_worker import compute_campaign_schedule
    for cid in sorted(touched_running):
        compute_campaign_schedule(db, cid, resume=True)
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="commit (default: dry run, rolls back)")
    args = ap.parse_args()
    db = SessionLocal()
    try:
        report = run(db, apply=args.apply)
    finally:
        db.close()
    print(("APPLIED" if args.apply else "DRY RUN (rolled back)") + f": {len(report)} campaigns")
    totals = defaultdict(int)
    for cid, r in sorted(report.items()):
        print(f"  campaign {cid}: " + ", ".join(f"{k}={v}" for k, v in sorted(r.items())))
        for k, v in r.items():
            if not k.startswith("status_"):
                totals[k] += v
    print("TOTAL: " + ", ".join(f"{k}={v}" for k, v in sorted(totals.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
