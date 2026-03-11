"""Gmail Send Service — Construct and send emails via the Gmail API."""

import base64
from email.mime.text import MIMEText
from typing import Dict, Any

import requests

from core.logger import get_logger

logger = get_logger(__name__)

GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


def send_gmail_email(
    access_token: str,
    to_email: str,
    subject: str,
    body: str,
    from_email: str = "me",
) -> Dict[str, Any]:
    """Send an email using the Gmail API.

    Args:
        access_token: Valid OAuth2 access token for the sender's Gmail account.
        to_email: Recipient email address.
        subject: Email subject line.
        body: Plain-text email body.
        from_email: Sender address (default "me" uses the authenticated account).

    Returns:
        Dict with Gmail API response (id, threadId, labelIds).

    Raises:
        RuntimeError: If the API call fails.
    """
    logger.info("Sending email to %s (subject: %s)", to_email, subject)

    # ── Build MIME message ───────────────────────────────────────────────
    message = MIMEText(body)
    message["to"] = to_email
    message["subject"] = subject
    if from_email != "me":
        message["from"] = from_email

    # Gmail API expects URL-safe base64-encoded raw message
    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

    # ── Send via Gmail API ───────────────────────────────────────────────
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {"raw": raw_message}

    resp = requests.post(GMAIL_SEND_URL, json=payload, headers=headers)

    if not resp.ok:
        logger.error("Gmail send failed: %d %s", resp.status_code, resp.text)
        raise RuntimeError(f"Gmail send failed: {resp.text}")

    result = resp.json()
    logger.info("Email sent successfully (id=%s, threadId=%s)", result.get("id"), result.get("threadId"))

    return result


def _refresh_token_sync(email_account, db) -> str:
    """Refresh an expired Gmail access token synchronously (for Celery workers).

    Returns the new access token, or the existing one if still valid.
    """
    from datetime import datetime, timedelta

    # Check if token is expired or about to expire (within 5 minutes)
    if email_account.token_expiry and email_account.token_expiry > datetime.utcnow() + timedelta(minutes=5):
        return email_account.access_token

    if not email_account.refresh_token:
        logger.warning("No refresh token for email account %d — using existing access token", email_account.id)
        return email_account.access_token

    logger.info("Refreshing expired Gmail token for account %d", email_account.id)

    from core.config import settings

    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": settings.GMAIL_CLIENT_ID,
        "client_secret": settings.GMAIL_CLIENT_SECRET,
        "refresh_token": email_account.refresh_token,
        "grant_type": "refresh_token",
    })

    if not resp.ok:
        logger.error("Token refresh failed: %d %s", resp.status_code, resp.text)
        return email_account.access_token

    token_data = resp.json()
    email_account.access_token = token_data["access_token"]
    email_account.token_expiry = datetime.utcnow() + timedelta(seconds=token_data.get("expires_in", 3599) - 60)
    if token_data.get("refresh_token"):
        email_account.refresh_token = token_data["refresh_token"]
    db.commit()

    logger.info("Token refreshed successfully for account %d", email_account.id)
    return email_account.access_token


def send_email_via_gmail(
    to_email: str,
    subject: str,
    body: str,
    email_account_id: int,
) -> bool:
    """Send email via Gmail using an EmailAccount's access token.

    Automatically refreshes expired tokens before sending.

    Args:
        to_email: Recipient email address.
        subject: Email subject line.
        body: Plain-text email body.
        email_account_id: ID of the EmailAccount to send from.

    Returns:
        True if email sent successfully, False otherwise.
    """
    from database.session import SessionLocal
    from database.models import EmailAccount

    db = SessionLocal()
    try:
        # Get email account
        email_account = db.query(EmailAccount).filter_by(id=email_account_id).first()
        if not email_account:
            logger.error("Email account %d not found", email_account_id)
            return False

        # Refresh token if expired
        access_token = _refresh_token_sync(email_account, db)

        # Send email
        send_gmail_email(
            access_token=access_token,
            to_email=to_email,
            subject=subject,
            body=body,
            from_email=email_account.email_address
        )
        return True

    except Exception as e:
        logger.error("Failed to send email to %s: %s", to_email, e)
        return False
    finally:
        db.close()
