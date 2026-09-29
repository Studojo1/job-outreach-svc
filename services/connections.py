"""Connected accounts: status and self-serve disconnect (Privacy Policy v2.0).

Disconnecting deletes what lets us act for the user: the Gmail OAuth tokens
(after revoking the grant with Google) and every stored LinkedIn cookie.

The email_accounts row is kept with its tokens blanked, not deleted:
campaigns.email_account_id cascades on delete, so removing the row would
delete the user's campaigns and their sent-email history. Reconnecting finds
the row by user_id and writes fresh tokens into it (token_manager.store_user_tokens).
"""
import logging
from datetime import datetime

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from database.models import Campaign, Candidate, EmailAccount, LinkedInCampaign, LinkedInToken

logger = logging.getLogger(__name__)

PAUSE_REASON_GMAIL_DISCONNECTED = "gmail_disconnected"

# Another service (the auto-apply workers) keeps LinkedIn cookies here, in
# the same database. Disconnecting LinkedIn from Studojo removes those too.
_EXTERNAL_LINKEDIN_TABLES = ("user_linkedin_sessions",)


def gmail_connected_filter():
    """SQL condition for a mailbox that still holds a token."""
    return EmailAccount.access_token != ""


def _gmail_accounts(db: Session, user_id: str):
    return db.query(EmailAccount).filter(EmailAccount.user_id == user_id, EmailAccount.provider == "gmail")


def _has_table(db: Session, name: str) -> bool:
    return inspect(db.connection()).has_table(name)  # the session's own connection, inside its transaction


def connection_status(db: Session, user_id: str) -> dict:
    mailbox = (
        _gmail_accounts(db, user_id).filter(gmail_connected_filter())
        .order_by(EmailAccount.created_at.desc()).first()
    )
    token = db.query(LinkedInToken).filter(LinkedInToken.user_id == user_id).first()
    method = None
    if token is not None:
        method = "extension" if token.connection_mode == "extension" else "password"
    else:
        for t in _EXTERNAL_LINKEDIN_TABLES:
            if _has_table(db, t) and db.execute(
                text(f'SELECT 1 FROM "{t}" WHERE user_id = :u AND is_active LIMIT 1'),  # noqa: S608 - t is a module constant
                {"u": user_id},
            ).first():
                method = "extension"  # auto-apply sessions are cookies from the browser extension
                break
    return {
        "gmail": {"connected": mailbox is not None, "email": mailbox.email_address if mailbox else None},
        "linkedin": {"connected": method is not None, "method": method},
    }


def disconnect_gmail(db: Session, user_id: str, revoke=None) -> dict:
    """Revoke the Google grant, blank the stored tokens, pause running campaigns.

    Idempotent: a second call finds no tokens and no running campaigns.
    Revoking is best effort (Google also answers 400 for an already-dead
    token); the tokens are deleted either way.
    """
    if revoke is None:
        from services.account_deletion import revoke_google_grant as revoke
    accounts = _gmail_accounts(db, user_id).all()
    for acct in accounts:
        token = acct.refresh_token or acct.access_token
        if token and not revoke(token):
            logger.warning("[DISCONNECT] Google revoke did not succeed for email_account %s; deleting tokens anyway",
                           acct.id)

    from services.email_campaign.outcomes import record_pause_event
    now = datetime.utcnow()
    paused = 0
    try:
        running = (
            db.query(Campaign).join(Candidate, Candidate.id == Campaign.candidate_id)
            .filter(Candidate.user_id == user_id, Campaign.status == "running").all()
        )
        for c in running:
            c.status = "paused"
            c.paused_at = now
            c.paused_by = "user"
            c.pause_reason = PAUSE_REASON_GMAIL_DISCONNECTED
            record_pause_event(db, c)
            paused += 1
        for acct in accounts:
            acct.access_token = ""  # column is NOT NULL; '' means disconnected
            acct.refresh_token = None
            acct.token_expiry = None
        db.commit()
    except Exception:
        db.rollback()
        raise
    if paused:
        from core.metrics import CAMPAIGNS_RUNNING
        CAMPAIGNS_RUNNING.dec(paused)
    logger.info("[DISCONNECT] Gmail disconnected for %s: %d mailbox(es), %d campaign(s) paused",
                user_id, len(accounts), paused)
    return {"disconnected": True, "campaigns_paused": paused}


def disconnect_linkedin(db: Session, user_id: str) -> dict:
    """Delete every stored LinkedIn cookie/session for the user and pause their
    running LinkedIn campaigns. We never store the LinkedIn password: it is
    used once to log in and discarded. Idempotent."""
    try:
        paused = 0
        for c in db.query(LinkedInCampaign).filter(LinkedInCampaign.user_id == user_id,
                                                   LinkedInCampaign.status == "running").all():
            c.status = "paused"
            c.updated_at = datetime.utcnow()
            paused += 1
        deleted = db.query(LinkedInToken).filter(LinkedInToken.user_id == user_id).delete(synchronize_session=False)
        for t in _EXTERNAL_LINKEDIN_TABLES:
            if _has_table(db, t):
                deleted += db.execute(text(f'DELETE FROM "{t}" WHERE user_id = :u'), {"u": user_id}).rowcount or 0  # noqa: S608 - module constant
        db.commit()
    except Exception:
        db.rollback()
        raise
    logger.info("[DISCONNECT] LinkedIn disconnected for %s: %d credential row(s) deleted, %d campaign(s) paused",
                user_id, deleted, paused)
    return {"disconnected": True}
