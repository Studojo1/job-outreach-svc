"""Tell customers what is happening to the campaign they paid for.

Nothing in the product ever contacted a paying customer about their campaign
(audit P09): a dead mailbox, a stalled campaign, a paused backlog and a
campaign that finished having delivered almost nothing were all silent, and
16 of the 19 paying users who got nothing never raised a ticket.

Hourly, with the launch sweep. Each notice goes out once per occurrence
(campaign_notices is the dedupe):

  gmail_reconnect   the worker paused the campaign because the mailbox lost
                    access (pause_reason='gmail_auth'). Link: reconnect Gmail,
                    which resumes the campaign.
  paused_reminder   paused by the customer with unsent work for 7+ days
                    (P38). Link: the dashboard, to resume. Pauses older than
                    60 days are listed for the founders instead.
  stalled           running with unsent work and nothing sent for 72 hours
                    (P42). The customer is told we are on it; the founders
                    get the list, since this is usually ours to fix.
  finished          completed, with the real split: delivered, skipped for
                    no email, credits returned (P20's "X of Y delivered").

Founders get one ops-alert per run that found something new to act on.
"""

from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.config import settings
from core.logger import get_logger
from database.models import Campaign, CampaignNotice, Candidate, EmailSent, User

logger = get_logger(__name__)

PAUSED_REMINDER_AFTER = timedelta(days=7)
PAUSED_REMINDER_MAX_AGE = timedelta(days=60)
STALL_AFTER = timedelta(hours=72)
# Campaigns that finished before notices existed are not emailed about it.
FINISHED_SINCE = datetime(2026, 9, 29)

UNSENT = ("pending_enrichment", "queued")


def _send(payload: dict) -> bool:
    from services.launch_nudge import _send_template
    return _send_template(payload)


def _owner(db: Session, campaign: Campaign) -> Optional[User]:
    return (
        db.query(User).join(Candidate, Candidate.user_id == User.id)
        .filter(Candidate.id == campaign.candidate_id).first()
    )


def _already(db: Session, campaign_id: int, kind: str, occurrence: str) -> bool:
    return db.query(CampaignNotice.id).filter_by(
        campaign_id=campaign_id, kind=kind, occurrence=occurrence).first() is not None


def _record(db: Session, campaign: Campaign, user: User, kind: str, occurrence: str, now: datetime) -> bool:
    db.add(CampaignNotice(campaign_id=campaign.id, user_id=user.id, kind=kind,
                          occurrence=occurrence, created_at=now))
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()  # another replica sent it
        return False


def _unsent(db: Session, campaign_id: int) -> int:
    return db.query(func.count(EmailSent.id)).filter(
        EmailSent.campaign_id == campaign_id, EmailSent.status.in_(UNSENT)).scalar() or 0


def _last_sent(db: Session, campaign: Campaign) -> Optional[datetime]:
    return db.query(func.max(EmailSent.sent_at)).filter(EmailSent.campaign_id == campaign.id).scalar()


def _url(path: str) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}{path}"


def _notify(db, campaign, kind, occurrence, now, payload, sent_log) -> None:
    if _already(db, campaign.id, kind, occurrence):
        return
    user = _owner(db, campaign)
    if user is None or not user.email:
        return
    payload = {"to": user.email, "user_name": (user.name or "").split(" ")[0] or "there", **payload}
    if _send(payload) and _record(db, campaign, user, kind, occurrence, now):
        sent_log.append((kind, user.email, campaign.id))


def run(db: Session, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    sent: List[tuple] = []
    founders: List[str] = []

    # 1. Gmail needs reconnecting
    for c in db.query(Campaign).filter(Campaign.status == "paused",
                                       Campaign.pause_reason == "gmail_auth").all():
        occurrence = (c.paused_at or c.created_at or now).isoformat(timespec="seconds")
        _notify(db, c, "gmail_reconnect", occurrence, now, {
            "template": "outreach-gmail-reconnect",
            "action_url": _url("/connect/gmail"),
            "credits": _unsent(db, c.id),
        }, sent)

    # 2. Paused by the customer, work waiting
    for c in db.query(Campaign).filter(Campaign.status == "paused").all():
        if c.pause_reason == "gmail_auth":
            continue
        paused_at = c.paused_at or c.created_at or now
        age = now - paused_at
        unsent = _unsent(db, c.id)
        if unsent == 0 or age < PAUSED_REMINDER_AFTER:
            continue
        if age > PAUSED_REMINDER_MAX_AGE:
            # Too old for an automatic email; a person should reach out.
            # Reported to the founders once (recorded, nothing is emailed).
            owner = _owner(db, c)
            if owner is not None and not _already(db, c.id, "paused_reminder", "founders") \
                    and _record(db, c, owner, "paused_reminder", "founders", now):
                founders.append(f"- paused {age.days}d: campaign {c.id} ({owner.email}), {unsent} unsent")
            continue
        _notify(db, c, "paused_reminder", paused_at.isoformat(timespec="seconds"), now, {
            "template": "outreach-campaign-paused",
            "action_url": _url("/campaign/dashboard"),
            "credits": unsent,
        }, sent)

    # 3. Running but not sending
    for c in db.query(Campaign).filter(Campaign.status == "running").all():
        unsent = _unsent(db, c.id)
        if unsent == 0:
            continue
        last = _last_sent(db, c) or c.started_at or c.created_at or now
        if now - last < STALL_AFTER:
            continue
        occurrence = last.isoformat(timespec="seconds")
        before = len(sent)
        _notify(db, c, "stalled", occurrence, now, {
            "template": "outreach-campaign-stalled",
            "action_url": _url("/campaign/dashboard"),
            "credits": unsent,
        }, sent)
        if len(sent) > before:
            owner = _owner(db, c)
            founders.append(f"- STALLED: campaign {c.id} ({owner.email if owner else '?'}) running, "
                            f"{unsent} unsent, nothing sent since {last:%Y-%m-%d %H:%M} UTC")

    # 4. Finished
    for c in db.query(Campaign).filter(Campaign.status == "completed",
                                       Campaign.completed_at >= FINISHED_SINCE).all():
        first = (EmailSent.campaign_id == c.id) & (EmailSent.followup_number == 0) & (EmailSent.is_test.isnot(True))
        delivered = db.query(func.count(EmailSent.id)).filter(
            first, EmailSent.status.in_(("sent", "replied", "bounced"))).scalar() or 0
        total = db.query(func.count(EmailSent.id)).filter(first).scalar() or 0
        replied = db.query(func.count(EmailSent.id)).filter(
            EmailSent.campaign_id == c.id, EmailSent.reply_received_at.isnot(None)).scalar() or 0
        _notify(db, c, "finished", "", now, {
            "template": "outreach-campaign-finished",
            "action_url": _url("/campaign/dashboard"),
            "delivered": delivered,
            "total": total,
            "replied": replied,
            "credits": c.credits_released or 0,
        }, sent)

    if founders:
        _alert_founders(sent, founders)
    if sent:
        logger.info("[NOTICES] sent %d: %s", len(sent), sent)
    return {"sent": sent, "founders": founders}


def _alert_founders(sent: list, founders: list) -> None:
    from services.launch_nudge import _env_tag
    lines = [f"- told {email} ({kind}) about campaign {cid}" for kind, email, cid in sent]
    message = (
        "Campaign notices this hour.\n\n"
        + ("Customers emailed:\n" + "\n".join(lines) + "\n\n" if lines else "")
        + ("Needs a person:\n" + "\n".join(founders) if founders else "")
    )
    for to in [a.strip() for a in settings.OPS_ALERT_RECIPIENTS.split(",") if a.strip()]:
        _send({"to": to, "template": "ops-alert",
               "subject": f"{_env_tag()}campaigns needing attention", "message": message})
