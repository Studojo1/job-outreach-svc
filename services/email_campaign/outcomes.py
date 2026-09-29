"""What happens to an email, its campaign and its credit when a send goes wrong.

Before this, every failure anywhere in the worker did `status = "failed"`.
That one line produced most of the post-payment audit's money findings:

  - P06: a transient token-refresh error (the message literally said "will
    retry next cycle") and a revoked Gmail grant were both terminal, so a dead
    mailbox burned an entire paid queue: campaign 100 lost 444 of 503 emails
    in 66 seconds, and reconnecting Gmail recovered none of them.
  - P13: the credit for a failed email was never returned (1,933 first
    touches for paying users).
  - P01/P20: failed rows count as finished work, so that campaign was then
    marked "completed".

Now there are three outcomes:

  retry    Google was down or throttling. The email goes back to `queued`
           a few minutes later. Nothing is lost.
  pause    The mailbox cannot send until its owner reconnects Gmail. The
           email goes back to `queued`, the campaign pauses with
           pause_reason='gmail_auth', and reconnecting resumes it.
  fail     This email can never be sent. It is marked failed and, if it held
           a paid slot that nothing else will fill, its credit is returned.

A credit comes back only for a first-touch email that held a paid slot:
  - not a test send, not a follow-up (neither was ever charged);
  - not a bounce replacement (the bounced original was delivered, so its
    slot was used, and the replacement is a free extra);
  - and only when no replacement lead was queued to fill the slot.
credits.release additionally caps the total at what the campaign reserved,
and campaigns reserved before the ledger existed (credits_reserved NULL)
never release automatically; they are settled by an audited backfill.
"""

from datetime import datetime, timedelta
from typing import Optional

import requests

from core.logger import get_logger
from database.models import Campaign, Candidate, EmailSent
from services import credits
from services.email_campaign.gmail_send_service import (
    GmailAuthError,
    GmailSendError,
    GmailTransientError,
)

logger = get_logger(__name__)

RETRY = "retry"
PAUSE = "pause"
FAIL = "fail"

RETRY_DELAY = timedelta(minutes=10)
PAUSE_REASON_GMAIL_AUTH = "gmail_auth"


def classify(exc: Exception, *, phase: str) -> str:
    """phase is 'refresh' (getting a token) or 'send' (the Gmail POST)."""
    if isinstance(exc, GmailAuthError):
        return PAUSE
    if isinstance(exc, GmailTransientError):
        return RETRY
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.ConnectTimeout)):
        # The request never reached Google, so nothing was sent.
        return RETRY
    if isinstance(exc, requests.exceptions.ReadTimeout):
        # A token call is safe to repeat. A send that timed out waiting for the
        # response may already have been delivered; retrying could send it twice.
        return RETRY if phase == "refresh" else FAIL
    if isinstance(exc, GmailSendError):
        return FAIL
    return FAIL


def _mark(email: EmailSent, status: str, message: Optional[str]) -> None:
    email.status = status
    if message is not None:
        email.error_message = message[:500]
    email.status_changed_at = datetime.utcnow()


def holds_paid_slot(email: EmailSent) -> bool:
    return (
        not email.is_test
        and (email.followup_number or 0) == 0
        and email.replacement_reason != "bounce"
    )


def fail(db, email: EmailSent, message: str, *, replaced: bool = False) -> int:
    """Mark the email failed for good. Returns credits released (0 or 1).
    `replaced`: a replacement lead was queued to fill this slot. Caller commits."""
    _mark(email, "failed", message)
    if replaced or not holds_paid_slot(email):
        return 0
    campaign = db.get(Campaign, email.campaign_id)
    if campaign is None or campaign.credits_reserved is None:
        return 0
    owner = db.query(Candidate.user_id).filter(Candidate.id == campaign.candidate_id).scalar()
    if owner is None:
        return 0
    return credits.release(db, owner, 1, credits.RELEASE_SEND_FAILED, campaign=campaign,
                           note=f"email {email.id}: {message[:120]}")


def retry(email: EmailSent, message: str) -> None:
    """Back to the queue, a little later. Caller commits."""
    _mark(email, "queued", message)
    email.scheduled_at = datetime.utcnow() + RETRY_DELAY


def pause_for_auth(db, campaign: Campaign, email: EmailSent, message: str,
                   *, followup: bool = False) -> None:
    """Keep the email, stop the campaign until Gmail is reconnected. Caller commits.

    A follow-up goes back to followup_pending, a first touch to queued, so the
    reconnect-and-resume path sends exactly what was waiting.
    """
    _mark(email, "followup_pending" if followup else "queued", message)
    if campaign.status == "running":
        campaign.status = "paused"
        campaign.paused_at = datetime.utcnow()
        campaign.paused_by = "system"
        campaign.pause_reason = PAUSE_REASON_GMAIL_AUTH
        record_pause_event(db, campaign)
        logger.warning("[OUTCOME] Campaign %d paused: Gmail needs reconnecting (%s)",
                       campaign.id, message[:160])


def record_pause_event(db, campaign: Campaign) -> None:
    """A system_events row per pause, for the pause history (audit P40).
    user_id stays NULL (the owner is in metadata) because the frontend shows
    a user their own system_events rows. Caller commits."""
    from database.models import SystemEvent
    owner = db.query(Candidate.user_id).filter(Candidate.id == campaign.candidate_id).scalar()
    db.add(SystemEvent(event_type="campaign_paused", user_id=None, created_at=datetime.utcnow(), meta={
        "campaign_id": campaign.id, "owner_user_id": owner,
        "paused_by": campaign.paused_by, "pause_reason": campaign.pause_reason,
    }))


def handle(db, campaign: Campaign, email: EmailSent, exc: Exception, *, phase: str,
           followup: bool = False) -> str:
    """Apply the right outcome for a failed refresh/send. Caller commits."""
    outcome = classify(exc, phase=phase)
    prefix = "Token refresh failed" if phase == "refresh" else "Send failed"
    message = f"{prefix}: {exc}"
    if outcome == PAUSE:
        pause_for_auth(db, campaign, email, message, followup=followup)
    elif outcome == RETRY:
        if followup:
            _mark(email, "followup_pending", message)
            email.scheduled_at = datetime.utcnow() + RETRY_DELAY
        else:
            retry(email, message)
    else:
        fail(db, email, message)
    return outcome
