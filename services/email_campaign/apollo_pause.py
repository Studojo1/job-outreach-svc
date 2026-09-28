"""Emails held back while Apollo is out of credits, and putting them back.

When JIT enrichment hits an Apollo credit/rate/5xx error the email is set to
enrichment_status='credit_paused' instead of failing (campaign_worker). This
module is the other half: it returns those rows to 'pending' once Apollo can
serve them again.

It used to requeue every paused row on every 30s cycle, credits or not, so
during an outage rows just cycled pending -> credit_paused, and because the
key manager never forgot an exhausted key, nothing recovered after a top-up
until the pod restarted. Now:

  - Rows are requeued only while the key manager has a usable key. An
    exhausted key is re-probed every EXHAUSTED_RETRY_AFTER (30 min), so a
    top-up is picked up on its own within that window, costing one Apollo
    call per probe while still empty.
  - signal_credits_restored() writes a system_events row. Every replica
    sees it on its next cycle, forgets its exhausted keys, and requeues at
    once instead of waiting for the probe.
  - A paused row's send slot usually passed while it waited. Campaigns
    with overdue rows are rescheduled from now at their normal daily pace,
    so a two-day backlog does not go out in one burst.

Apollo does not always say it is out of credits. In the 2026-09-27/28
outage People Match answered 200 with no email, which reads exactly like
"this person has no email": 147 paid emails failed as no-match (the normal
rate is 0-5 a day). apollo_looks_empty() is the check made before failing
an email for good: it re-asks Apollo for leads it recently found. If it
cannot find those either, the account is empty, not the lead.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.logger import get_logger
from database.models import Campaign, EmailSent, Lead, SystemEvent
from services.shared.apollo_key_manager import apollo_keys

logger = get_logger(__name__)

CREDITS_RESTORED_EVENT = "apollo_credits_restored"
PAUSED = "credit_paused"
# A row this late is treated as having missed its slot, not as normal jitter.
OVERDUE_AFTER = timedelta(minutes=15)

CANARY_COUNT = 2  # recently found leads re-asked before calling Apollo empty
CANARY_MAX_AGE = timedelta(days=7)
CANARY_VERDICT_TTL = timedelta(minutes=10)
_canary_verdict: Optional[tuple[datetime, bool]] = None

# Per process: the newest restore signal this replica has acted on.
_seen_restore_at: Optional[datetime] = None
_restore_checked = False


def signal_credits_restored(db: Session, note: str = "") -> None:
    """Record that Apollo was topped up. Caller commits."""
    db.add(SystemEvent(event_type=CREDITS_RESTORED_EVENT, meta={"note": note} if note else None))
    apollo_keys.reset()


def _apply_restore_signal(db: Session) -> None:
    """Forget exhausted keys if another replica (or an admin) signalled a top-up."""
    global _seen_restore_at, _restore_checked
    latest = (
        db.query(func.max(SystemEvent.created_at))
        .filter(SystemEvent.event_type == CREDITS_RESTORED_EVENT)
        .scalar()
    )
    if not _restore_checked:
        # A fresh process has no exhausted keys; older signals are already moot.
        _restore_checked = True
        _seen_restore_at = latest
        return
    if latest is not None and (_seen_restore_at is None or latest > _seen_restore_at):
        apollo_keys.reset()
        _seen_restore_at = latest


def apollo_looks_empty(db: Session, exclude_lead_id: Optional[int] = None) -> bool:
    """True if Apollo cannot find even leads it found recently.

    Re-matches up to CANARY_COUNT recently enriched leads (already unlocked,
    so normally no new credit). Empty only if every one of them fails; one
    success means the no-match is about the lead. The verdict is cached for
    CANARY_VERDICT_TTL. When empty, the current key is marked exhausted so
    the rest of the queue pauses without calling Apollo, and the normal
    re-probe / restore path takes over.
    """
    global _canary_verdict
    from services.enrichment.enrichment_service import enrich_single_lead_classified

    now = datetime.utcnow()
    if _canary_verdict and now - _canary_verdict[0] < CANARY_VERDICT_TTL:
        return _canary_verdict[1]

    q = (
        db.query(Lead)
        .join(EmailSent, EmailSent.lead_id == Lead.id)
        .filter(
            EmailSent.enrichment_status == "enriched",
            EmailSent.scheduled_at >= now - CANARY_MAX_AGE,
            Lead.email.isnot(None),
            Lead.email_verified.is_(True),
        )
        .order_by(EmailSent.scheduled_at.desc())
    )
    if exclude_lead_id is not None:
        q = q.filter(Lead.id != exclude_lead_id)
    canaries, seen = [], set()
    for lead in q.limit(20):
        if lead.id not in seen:
            seen.add(lead.id)
            canaries.append(lead)
        if len(canaries) == CANARY_COUNT:
            break
    if not canaries:
        return False  # nothing to compare against; trust the no-match

    empty = all(not enrich_single_lead_classified(c).success for c in canaries)
    # enrich_single_lead_classified never writes, but do not let a canary
    # object carry anything into the caller's commit.
    for c in canaries:
        db.expire(c)
    _canary_verdict = (now, empty)
    if empty:
        key = apollo_keys.get_key()
        if key:
            apollo_keys.report_failure(key, 402)
        logger.warning("[APOLLO-PAUSE] Apollo returned nothing for %d recently found lead(s): "
                       "treating as out of credits", len(canaries))
    return empty


def paused_count(db: Session) -> int:
    return db.query(func.count(EmailSent.id)).filter(EmailSent.enrichment_status == PAUSED).scalar() or 0


def _reschedule_overdue(db: Session, campaign_ids: set[int]) -> None:
    from services.email_campaign.campaign_worker import compute_campaign_schedule

    cutoff = datetime.utcnow() - OVERDUE_AFTER
    for cid in campaign_ids:
        overdue = (
            db.query(EmailSent.id)
            .join(Campaign, Campaign.id == EmailSent.campaign_id)
            .filter(
                Campaign.id == cid,
                Campaign.status == "running",
                EmailSent.status.in_(["pending_enrichment", "queued"]),
                EmailSent.scheduled_at < cutoff,
            )
            .first()
        )
        if overdue:
            compute_campaign_schedule(db, cid, resume=True)


def requeue_credit_paused(db: Session, campaign_id: Optional[int] = None) -> int:
    """Put credit_paused rows back to 'pending' if Apollo looks usable again.

    Pass campaign_id to scope to one campaign, or None for all (the per-cycle
    sweep). Returns the number of rows requeued.
    """
    _apply_restore_signal(db)
    if not apollo_keys.has_valid_key():
        return 0

    q = db.query(EmailSent).filter(EmailSent.enrichment_status == PAUSED)
    if campaign_id is not None:
        q = q.filter(EmailSent.campaign_id == campaign_id)
    campaign_ids = {cid for (cid,) in q.with_entities(EmailSent.campaign_id).distinct()}
    if not campaign_ids:
        return 0

    affected = q.update(
        {EmailSent.enrichment_status: "pending", EmailSent.error_message: None},
        synchronize_session=False,
    )
    db.commit()
    _reschedule_overdue(db, campaign_ids)
    logger.info("[APOLLO-PAUSE] Requeued %d credit_paused emails across %d campaign(s)",
                affected, len(campaign_ids))
    return affected
