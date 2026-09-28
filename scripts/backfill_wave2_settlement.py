"""One-off: settle the damage the wave-2 fixes stop from recurring.

Dry run by default; prints every change per user. --apply commits it all in
one transaction, and every credit change is a credit_ledger row with
actor='backfill-wave2', so the whole run is auditable and reversible.

Per campaign, in order:

 1. credits_reserved for campaigns created before the ledger (it is NULL).
    Per user, oldest campaign first, each gets min(its paid first touches,
    what is left of the user's used_credits). That caps campaigns holding
    rows nobody paid for (campaign 124: 1,064 rows against 200 credits) at
    what was actually reserved, so no release below can mint credits.
 2. Gmail-auth failures (P06). First touches that failed only because the
    mailbox lost access are put back in the queue. If the campaign had been
    marked completed on top of them it is reopened as paused with
    pause_reason='gmail_auth', so Resume, or reconnecting Gmail, sends them.
 3. Other failed paid first touches (P13) return their credit, unless a
    replacement lead took the slot.
 4. Completed/cancelled campaigns still holding unsent paid work (P04/P47):
    the work is retired as 'expired' and its credits return.
 5. Orders whose campaign has ended move to 'completed' (P10).

Paused campaigns keep their reservations: that work is still owed, and
resuming now works (P02).

 6. After the commit, every running campaign with unsent work is re-planned
    from now (compute_campaign_schedule). Requeued rows are parked with no
    send time until then, so they can never all fire at once from a
    student's Gmail. This also moves campaigns still on the pre-2026-09-19
    5-7/day schedule onto their daily_limit (P25: campaign 124's last email
    was due 2027-02-13).

Usage (inside a job-outreach-svc pod, or anywhere with DATABASE_URL):
    python -m scripts.backfill_wave2_settlement            # dry run
    python -m scripts.backfill_wave2_settlement --apply
"""

import argparse
import sys
from collections import defaultdict
from datetime import datetime

from sqlalchemy import or_

from database.models import Campaign, Candidate, EmailSent, OutreachOrder, UserCredit
from database.session import SessionLocal
from services import credits
from services.email_campaign.outcomes import PAUSE_REASON_GMAIL_AUTH, holds_paid_slot

ACTOR = "backfill-wave2"
UNSENT = ("pending_enrichment", "queued")


def _is_auth_failure(email: EmailSent) -> bool:
    msg = (email.error_message or "").lower()
    return (msg.startswith("token refresh failed") or "gmail auth expired" in msg
            or '"code": 401' in msg or "insufficient authentication scopes" in msg)


def _owner(db, campaign):
    return db.query(Candidate.user_id).filter(Candidate.id == campaign.candidate_id).scalar()


def run(db, apply: bool) -> dict:
    now = datetime.utcnow()
    report = defaultdict(lambda: defaultdict(int))

    campaigns = db.query(Campaign).order_by(Campaign.created_at.asc(), Campaign.id.asc()).all()
    by_user = defaultdict(list)
    for c in campaigns:
        uid = _owner(db, c)
        if uid:
            by_user[uid].append(c)

    replaced_ids = {
        rid for (rid,) in db.query(EmailSent.replacement_for_id)
        .filter(EmailSent.replacement_for_id.isnot(None))
    }

    for uid, user_campaigns in by_user.items():
        wallet = db.query(UserCredit).filter_by(user_id=uid).first()
        budget = wallet.used_credits if wallet else 0

        for c in user_campaigns:
            rows = db.query(EmailSent).filter(EmailSent.campaign_id == c.id).all()
            paid = [r for r in rows if holds_paid_slot(r) and r.replacement_for_id is None]

            # 1. reservation for pre-ledger campaigns
            if c.credits_reserved is None:
                c.credits_reserved = max(0, min(len(paid), budget))
                c.credits_released = c.credits_released or 0
                report[uid]["reserved_backfilled"] += c.credits_reserved
            budget = max(0, budget - (c.credits_reserved - (c.credits_released or 0)))

            # 2. Gmail-auth failures go back in the queue
            auth_failed = [r for r in rows if r.status == "failed" and _is_auth_failure(r)
                           and (r.followup_number or 0) == 0 and not r.is_test]
            if auth_failed:
                for r in auth_failed:
                    r.status = "queued"
                    r.scheduled_at = None  # re-planned after commit (step 6)
                    r.status_changed_at = now
                    r.error_message = f"Requeued by {ACTOR}: was {r.error_message[:200]}"
                report[uid]["auth_requeued"] += len(auth_failed)
                if c.status == "completed":
                    c.status = "paused"
                    c.paused_at = now
                    c.paused_by = ACTOR
                    c.pause_reason = PAUSE_REASON_GMAIL_AUTH
                    report[uid]["campaigns_reopened"] += 1

            # 3. other failed paid first touches return their credit
            lost = [r for r in rows if r.status == "failed" and holds_paid_slot(r)
                    and r.id not in replaced_ids]
            if lost:
                n = credits.release(db, uid, len(lost), credits.RELEASE_SEND_FAILED,
                                    campaign=c, actor=ACTOR,
                                    note=f"{len(lost)} failed first touches before wave 2")
                report[uid]["released_failed"] += n

            # 4. ended campaigns still holding unsent paid work
            if c.status in ("completed", "cancelled"):
                unsent = [r for r in rows if r.status in UNSENT]
                unsent_paid = sum(1 for r in unsent if holds_paid_slot(r))
                for r in unsent:
                    r.status = "expired"
                    r.status_changed_at = now
                    r.error_message = f"Campaign had ended; retired by {ACTOR}"
                for r in rows:
                    if r.status == "followup_pending":
                        r.status = "cancelled_expired"
                        r.status_changed_at = now
                if unsent_paid:
                    n = credits.release(db, uid, unsent_paid, credits.RELEASE_CAMPAIGN_FINISHED,
                                        campaign=c, actor=ACTOR,
                                        note=f"{unsent_paid} unsent emails on a {c.status} campaign")
                    report[uid]["released_unsent"] += n

            # 5. close the order of an ended campaign
            if c.status in ("completed", "cancelled"):
                orders = db.query(OutreachOrder).filter(
                    or_(OutreachOrder.id == c.outreach_order_id, OutreachOrder.campaign_id == c.id),
                    OutreachOrder.status != "completed",
                ).all()
                for o in orders:
                    o.status = "completed"
                    log = list(o.action_log or [])
                    log.append({"ts": now.isoformat(), "msg": f"Completed by {ACTOR}: campaign {c.id} is {c.status}"})
                    o.action_log = log
                    o.updated_at = now
                    report[uid]["orders_completed"] += 1

    running_with_work = [
        cid for (cid,) in db.query(Campaign.id).filter(Campaign.status == "running")
        .filter(Campaign.id.in_(db.query(EmailSent.campaign_id).filter(EmailSent.status.in_(UNSENT))))
    ]
    report["_"]["campaigns_replanned"] = len(running_with_work)

    if not apply:
        db.rollback()
        return report
    db.commit()

    # 6. re-plan running campaigns from now (commits per campaign)
    from services.email_campaign.campaign_worker import compute_campaign_schedule
    for cid in running_with_work:
        compute_campaign_schedule(db, cid)
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

    from database.models import User
    db = SessionLocal()
    emails = {u.id: u.email for u in db.query(User).filter(User.id.in_(list(report.keys())))}
    db.close()
    keys = ("reserved_backfilled", "auth_requeued", "campaigns_reopened",
            "released_failed", "released_unsent", "orders_completed", "campaigns_replanned")
    totals = defaultdict(int)
    print(("APPLIED" if args.apply else "DRY RUN (rolled back)") + f": {len(report)} users")
    for uid, r in sorted(report.items(), key=lambda kv: -(kv[1]["released_failed"] + kv[1]["released_unsent"])):
        if not any(r[k] for k in keys[1:]):
            for k in keys:
                totals[k] += r[k]
            continue
        print(f"  {emails.get(uid, uid)}: " + ", ".join(f"{k}={r[k]}" for k in keys if r[k]))
        for k in keys:
            totals[k] += r[k]
    print("TOTAL: " + ", ".join(f"{k}={totals[k]}" for k in keys))
    return 0


if __name__ == "__main__":
    sys.exit(main())
