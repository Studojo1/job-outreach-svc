"""Gmail Send Service — Construct and send emails via the Gmail API."""

import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from html import escape
from typing import Dict, Any, Optional

import requests

from core.logger import get_logger
from core.metrics import EMAILS_SENT_TOTAL

logger = get_logger(__name__)

GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
class GmailAuthError(RuntimeError):
    """The mailbox cannot send until its owner reconnects Gmail: the refresh
    token was revoked/expired, or the grant lacks the send scope. Retrying
    will not help; the campaign pauses and waits for a reconnect."""


class GmailTransientError(RuntimeError):
    """Google was unavailable or throttling (token endpoint 5xx, send 429/5xx).
    The email was not sent and is safe to retry next cycle."""


class GmailSendError(RuntimeError):
    """Gmail rejected this one message. `status` is the HTTP status."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


# (connect, read) seconds. requests has no default, so without this one hung
# connection to Google stalls the whole send cycle with no ceiling.
GMAIL_HTTP_TIMEOUT = (5, 30)
GMAIL_MESSAGE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"


def _build_html_body(body: str, pixel_url: str) -> str:
    """Wrap a plain-text body as HTML and append a 1x1 tracking pixel.

    The visible text is HTML-escaped and newlines become <br> so the HTML part
    renders the same as the plain-text alternative. The pixel is a hidden 1x1
    image whose load is recorded by the tracking endpoint.
    """
    safe = escape(body).replace("\n", "<br>\n")
    pixel = (
        f'<img src="{escape(pixel_url, quote=True)}" width="1" height="1" '
        f'style="display:none;max-height:0;overflow:hidden" alt="">'
    )
    return f'<html><body>{safe}{pixel}</body></html>'


def send_gmail_email(
    access_token: str,
    to_email: str,
    subject: str,
    body: str,
    from_email: str = "me",
    thread_id: Optional[str] = None,
    in_reply_to_header: Optional[str] = None,
    pixel_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Send an email using the Gmail API.

    For follow-ups: pass `thread_id` (from the original email) to reply in-thread.
    Also pass `in_reply_to_header` so non-Gmail clients thread the conversation
    correctly via RFC 5322 headers.

    Args:
        access_token: Valid OAuth2 access token for the sender's Gmail account.
        to_email: Recipient email address.
        subject: Email subject line.
        body: Plain-text email body.
        from_email: Sender address (default "me" uses the authenticated account).
        thread_id: Gmail threadId to reply into (optional, for follow-ups).
        in_reply_to_header: The original Message-ID header value, e.g. "<abc@mail.gmail.com>".
        pixel_url: Open-tracking pixel URL. When given, the message is sent as
            multipart/alternative (text/plain + text/html) with a hidden 1x1
            pixel in the HTML part. When None, sends plain text only.

    Returns:
        Dict with Gmail API response (id, threadId, labelIds).

    Raises:
        RuntimeError: If the API call fails.
        SuppressedAddress: If the recipient is on the suppression list.
    """
    # Last line for every Gmail send, whichever path got here: never write to
    # an address that bounced or asked to be removed (Privacy Policy §5).
    from services.email_campaign.suppression import guard_send
    guard_send(to_email)

    logger.info("Sending email to %s (subject: %s, thread_id=%s)", to_email, subject, thread_id)

    # ── Build MIME message ───────────────────────────────────────────────
    # With a pixel: multipart/alternative so clients that block images still
    # see the plain-text part; the HTML part carries the tracking pixel.
    # Without: plain text only (unchanged legacy behaviour, e.g. test emails).
    if pixel_url:
        message = MIMEMultipart("alternative")
        message.attach(MIMEText(body, "plain"))
        message.attach(MIMEText(_build_html_body(body, pixel_url), "html"))
    else:
        message = MIMEText(body)
    message["to"] = to_email
    message["subject"] = subject
    if from_email != "me":
        message["from"] = from_email

    # Threading headers for follow-up replies
    if in_reply_to_header:
        message["In-Reply-To"] = in_reply_to_header
        message["References"] = in_reply_to_header

    # Gmail API expects URL-safe base64-encoded raw message
    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

    # ── Send via Gmail API ───────────────────────────────────────────────
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {"raw": raw_message}
    if thread_id:
        payload["threadId"] = thread_id

    resp = requests.post(GMAIL_SEND_URL, json=payload, headers=headers, timeout=GMAIL_HTTP_TIMEOUT)

    if not resp.ok:
        EMAILS_SENT_TOTAL.labels(status="failed").inc()
        logger.error("Gmail send failed: %d %s", resp.status_code, resp.text)
        message = f"Gmail send failed: {resp.status_code}: {resp.text}"
        if resp.status_code == 401 or (
            resp.status_code == 403 and "insufficient authentication scopes" in resp.text.lower()
        ):
            raise GmailAuthError(message)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise GmailTransientError(message)
        raise GmailSendError(message, resp.status_code)

    result = resp.json()
    EMAILS_SENT_TOTAL.labels(status="sent").inc()
    logger.info("Email sent successfully (id=%s, threadId=%s)", result.get("id"), result.get("threadId"))

    return result


def fetch_message_id_header(access_token: str, gmail_message_id: str) -> Optional[str]:
    """Fetch the RFC 5322 Message-ID header for a sent Gmail message.

    This header (e.g. "<CA+xyz@mail.gmail.com>") is needed as the In-Reply-To
    header when sending follow-up replies so non-Gmail clients thread correctly.

    Args:
        access_token: Valid OAuth2 access token.
        gmail_message_id: The Gmail API message ID returned by send (not the header).

    Returns:
        The Message-ID header value (including angle brackets), or None on failure.
    """
    url = f"{GMAIL_MESSAGE_URL}/{gmail_message_id}?format=metadata&metadataHeaders=Message-Id"
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if not resp.ok:
            logger.warning("Failed to fetch Message-Id header for %s: %d", gmail_message_id, resp.status_code)
            return None

        payload_headers = resp.json().get("payload", {}).get("headers", [])
        for h in payload_headers:
            if h.get("name", "").lower() == "message-id":
                return h.get("value")
        return None
    except Exception as e:
        logger.warning("Exception fetching Message-Id for %s: %s", gmail_message_id, e)
        return None


def find_sent_message(access_token: str, to_email: str, after_epoch: int):
    """Look in the Sent folder for a message to `to_email` since `after_epoch`.

    Returns the newest {id, threadId} if found, {} if Gmail says there is none,
    or None if the search itself failed (the caller must not guess then).
    Used to settle emails stuck in 'sending' without sending them twice.
    """
    try:
        resp = requests.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"q": f"in:sent to:{to_email} after:{int(after_epoch)}", "maxResults": 5},
            timeout=GMAIL_HTTP_TIMEOUT,
        )
    except requests.RequestException:
        return None
    if not resp.ok:
        return None
    messages = resp.json().get("messages") or []
    return messages[0] if messages else {}


def _refresh_token_sync(email_account, db) -> str:
    """Refresh an expired Gmail access token synchronously (for Celery workers).

    Returns the new access token, or the existing one if still valid.
    """
    from datetime import datetime, timedelta

    # Check if token is expired or about to expire (within 5 minutes)
    if email_account.token_expiry and email_account.token_expiry > datetime.utcnow() + timedelta(minutes=5):
        return email_account.access_token

    if not email_account.refresh_token:
        logger.error(
            "[GMAIL-AUTH] No refresh token for account %d, user must reconnect Gmail",
            email_account.id,
        )
        raise GmailAuthError(
            f"Gmail auth expired for account {email_account.id}: the owner must reconnect their Gmail account"
        )

    logger.info("Refreshing expired Gmail token for account %d", email_account.id)

    from core.config import settings

    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": settings.GMAIL_CLIENT_ID,
        "client_secret": settings.GMAIL_CLIENT_SECRET,
        "refresh_token": email_account.refresh_token,
        "grant_type": "refresh_token",
    }, timeout=GMAIL_HTTP_TIMEOUT)

    if not resp.ok:
        try:
            err_body = resp.json()
            err_code = err_body.get("error", "")
        except Exception:
            err_code = ""
            err_body = resp.text

        # "invalid_grant" means the refresh token was revoked or expired by Google
        # (e.g. user disconnected the app, or 7-day unverified-app limit hit).
        # Any other error (5xx, quota, etc.) is transient.
        if err_code == "invalid_grant":
            # Log the account id only (audit PS-N14: each attempt wrote the
            # user's address three times). Mark the mailbox dead so the reply
            # check stops refreshing it every 5 minutes until they reconnect.
            logger.error(
                "[GMAIL-AUTH] Refresh token revoked/expired for account %d; user must reconnect Gmail",
                email_account.id,
            )
            try:
                email_account.token_invalid_at = datetime.utcnow()
                db.commit()
            except Exception:
                db.rollback()
            raise GmailAuthError(
                f"Gmail auth expired for account {email_account.id}: the owner must reconnect their Gmail account"
            )
        else:
            logger.error(
                "[GMAIL-AUTH] Transient token refresh failure for account %d: HTTP %d %s",
                email_account.id, resp.status_code, err_body,
            )
            raise GmailTransientError(
                f"Gmail token refresh failed (HTTP {resp.status_code}) — will retry next cycle"
            )

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
