"""Paid-not-launched sweep: nobody who pays is left to find their own way back.

The routing fix (services/next_step.py) sends a paid user to Launch whenever
they come back. This covers the ones who don't come back: 16 of the 19
zero-delivery payers in the audit never raised a ticket, they just left.

Hourly, from the worker cycle:
  - every user in a PAID_NOT_LAUNCHED state whose payment is older than
    FIRST_NUDGE_AFTER gets an outreach-launch-nudge email with a one-click
    link to the step that is blocking them, at most MAX_NUDGES times, spaced
    by NUDGE_GAPS;
  - the founders get one ops-alert listing who was nudged and everyone still
    stuck, so a pattern (a broken setup page) is seen the same day, not in
    the next audit.

Every send is recorded in launch_nudges, which is also the dedupe: a pod
restart or a second replica cannot double-send.
"""

from datetime import datetime, timedelta
from typing import List, Optional

import requests
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from core.config import settings
from core.logger import get_logger
from database.models import Campaign, Candidate, LaunchNudge, PaymentOrder, SystemEvent, User, UserCredit
from services.credits import MIN_CAMPAIGN_CREDITS
from services.next_step import PAID_NOT_LAUNCHED, resolve_next_step

logger = get_logger(__name__)

SWEEP_EVENT = "launch_nudge_sweep"
DIGEST_EVENT = "launch_stall_digest"
DIGEST_EVERY = timedelta(hours=24)
SWEEP_EVERY = timedelta(hours=1)
FIRST_NUDGE_AFTER = timedelta(hours=2)
# Gap before nudge 2 and nudge 3, measured from the previous nudge.
NUDGE_GAPS = (timedelta(hours=24), timedelta(hours=72))
MAX_NUDGES = 1 + len(NUDGE_GAPS)
# Older payments are listed for the founders but never auto-emailed: a
# "you haven't launched" email months after payment needs a human.
AUTO_NUDGE_MAX_AGE = timedelta(days=14)
EMAILER_TIMEOUT = (5, 15)

_STATE_LABEL = {
    "launch_ready": "ready to launch",
    "launch_draft": "campaign created, never sent",
    "connect_gmail": "needs Gmail",
    "needs_profile": "needs a finished profile",
}


def _send_template(payload: dict) -> bool:
    if not settings.EMAILER_INTERNAL_SECRET:
        return False
    try:
        resp = requests.post(
            f"{settings.EMAILER_URL.rstrip('/')}/v1/email/send-template",
            json=payload,
            headers={"X-Internal-Secret": settings.EMAILER_INTERNAL_SECRET},
            timeout=EMAILER_TIMEOUT,
        )
        if resp.ok:
            return True
        logger.error("[LAUNCH-NUDGE] emailer %s: %s", resp.status_code, resp.text[:300])
    except requests.RequestException as e:
        logger.error("[LAUNCH-NUDGE] emailer unreachable: %s", e)
    return False


def _candidate_user_ids(db: Session) -> List[str]:
    """Users who might be paid-not-launched: spendable credits, or a draft."""
    with_credits = [
        uid for (uid,) in db.query(UserCredit.user_id)
        .filter(UserCredit.total_credits - UserCredit.used_credits >= MIN_CAMPAIGN_CREDITS)
    ]
    with_drafts = [
        uid for (uid,) in db.query(Candidate.user_id)
        .join(Campaign, Campaign.candidate_id == Candidate.id)
        .filter(Campaign.status == "draft")
        .distinct()
    ]
    return sorted(set(with_credits) | set(with_drafts))


def _last_paid_at(db: Session, user_id: str) -> Optional[datetime]:
    return (
        db.query(func.max(PaymentOrder.created_at))
        .filter(PaymentOrder.user_id == user_id,
                PaymentOrder.status.in_(("paid", "completed")),
                # Real money only. 100%-coupon accounts include the Google
                # OAuth reviewers' OAUTH100 logins, which must never be mailed.
                PaymentOrder.amount_cents > 0)
        .scalar()
    )


def _launched_since(db: Session, user_id: str, since: datetime) -> bool:
    return (
        db.query(Campaign.id)
        .join(Candidate, Candidate.id == Campaign.candidate_id)
        .filter(Candidate.user_id == user_id,
                Campaign.status != "draft",
                Campaign.created_at >= since)
        .first()
    ) is not None


def _nudges_so_far(db: Session, user_id: str, since: datetime):
    """Nudges sent for the current payment (a new purchase restarts the count)."""
    return (
        db.query(LaunchNudge)
        .filter(LaunchNudge.user_id == user_id, LaunchNudge.created_at >= since)
        .order_by(LaunchNudge.created_at.asc())
        .all()
    )


def _due(nudges, paid_at: datetime, now: datetime) -> bool:
    if now - paid_at > AUTO_NUDGE_MAX_AGE:
        return False
    n = len(nudges)
    if n >= MAX_NUDGES:
        return False
    if n == 0:
        return now - paid_at >= FIRST_NUDGE_AFTER
    return now - nudges[-1].created_at >= NUDGE_GAPS[n - 1]


def sweep(db: Session, now: Optional[datetime] = None, send: bool = True) -> dict:
    """One pass. Returns what it found and did (also used for dry runs)."""
    now = now or datetime.utcnow()
    stuck, nudged = [], []

    for user_id in _candidate_user_ids(db):
        step = resolve_next_step(db, user_id, heal=False)
        if step.state not in PAID_NOT_LAUNCHED:
            continue
        paid_at = _last_paid_at(db, user_id)
        if paid_at is None:
            # Credits with no payment (comps, P29) are not this sweep's business.
            continue
        if _launched_since(db, user_id, paid_at):
            # Paid, launched, and has credits left (a finished campaign gave
            # back its unsent work, or they bought extra). Not stuck.
            continue
        user = db.get(User, user_id)
        if user is None or not user.email:
            continue
        nudges = _nudges_so_far(db, user_id, paid_at)
        row = {
            "user_id": user_id, "email": user.email, "state": step.state,
            "credits": step.available_credits, "paid_at": paid_at,
            "nudges": len(nudges),
        }
        stuck.append(row)

        if not send or not _due(nudges, paid_at, now):
            continue
        action_url = f"{settings.FRONTEND_URL.rstrip('/')}{step.path}"
        ok = _send_template({
            "to": user.email,
            "template": "outreach-launch-nudge",
            "user_name": (user.name or "").split(" ")[0] or "there",
            "credits": step.available_credits if step.state != "launch_draft" else 0,
            "action_url": action_url,
        })
        if ok:
            db.add(LaunchNudge(user_id=user_id, n=len(nudges) + 1, state=step.state,
                               action_url=action_url, created_at=now))
            db.commit()
            row["nudges"] += 1
            nudged.append(row)

    if send and stuck and (nudged or _digest_due(db, now)) and _alert_founders(stuck, nudged):
        db.add(SystemEvent(event_type=DIGEST_EVENT, created_at=now,
                           meta={"stuck": len(stuck), "nudged": len(nudged)}))
        db.commit()
    return {"stuck": stuck, "nudged": nudged}


def _digest_due(db: Session, now: datetime) -> bool:
    """Founders hear about everyone stuck at least daily, including payers too
    old to auto-nudge, who would otherwise never trigger an alert."""
    last = (
        db.query(func.max(SystemEvent.created_at))
        .filter(SystemEvent.event_type == DIGEST_EVENT)
        .scalar()
    )
    return last is None or now - last >= DIGEST_EVERY


def _env_tag() -> str:
    """Staging runs this sweep too, on its own test data; say so in the subject."""
    return "" if "studojo.com" in settings.FRONTEND_URL else "[staging] "


def _alert_founders(stuck: list, nudged: list) -> bool:
    """True if at least one founder was emailed."""
    def line(r):
        days = (datetime.utcnow() - r["paid_at"]).days
        return (f"- {r['email']}: {_STATE_LABEL.get(r['state'], r['state'])}, "
                f"{r['credits']} credits, paid {days}d ago, nudges sent {r['nudges']}/{MAX_NUDGES}")

    head = (
        f"{len(nudged)} paying user(s) were just emailed a Launch link because they paid "
        f"and never launched a campaign.\n\n" + "\n".join(line(r) for r in nudged) + "\n\n"
        if nudged else "Daily check: paying users who have not launched a campaign.\n\n"
    )
    message = (
        head
        + f"Everyone currently paid and not launched ({len(stuck)}):\n"
        + "\n".join(line(r) for r in stuck)
        + f"\n\nNudges stop after {MAX_NUDGES}, and payments older than {AUTO_NUDGE_MAX_AGE.days} days "
        "are never auto-emailed. Anyone still on this list needs a personal message."
    )
    sent = False
    for to in [a.strip() for a in settings.OPS_ALERT_RECIPIENTS.split(",") if a.strip()]:
        sent = _send_template({
            "to": to,
            "template": "ops-alert",
            "subject": f"{_env_tag()}{len(stuck)} paid user(s) have not launched",
            "message": message,
        }) or sent
    return sent


def maybe_sweep(db: Session) -> Optional[dict]:
    """Run the sweep if an hour has passed since the last one. Safe to call
    every worker cycle, from any replica."""
    try:
        # One sweeper at a time across replicas; released at commit/rollback.
        if db.bind.dialect.name == "postgresql":
            got = db.execute(text("SELECT pg_try_advisory_xact_lock(hashtext('launch_nudge_sweep'))")).scalar()
            if not got:
                db.rollback()
                return None
        last = (
            db.query(func.max(SystemEvent.created_at))
            .filter(SystemEvent.event_type == SWEEP_EVENT)
            .scalar()
        )
        now = datetime.utcnow()
        if last is not None and now - last < SWEEP_EVERY:
            db.rollback()
            return None
        db.add(SystemEvent(event_type=SWEEP_EVENT, created_at=now))
        db.commit()
        from services import campaign_notices, reconcile
        reconcile.run(db, now=now)
        try:
            campaign_notices.run(db, now=now)
        except Exception:
            db.rollback()
            logger.exception("[NOTICES] run failed")
        try:
            from services import retention
            retention.run(db, now=now)
        except Exception:
            db.rollback()
            logger.exception("[RETENTION] run failed")
        result = sweep(db, now=now)
        if result["nudged"]:
            logger.info("[LAUNCH-NUDGE] nudged %d, %d paid-not-launched",
                        len(result["nudged"]), len(result["stuck"]))
        return result
    except Exception:
        db.rollback()
        logger.exception("[LAUNCH-NUDGE] sweep failed")
        return None
