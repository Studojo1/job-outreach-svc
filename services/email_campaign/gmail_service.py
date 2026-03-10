"""Gmail Service — Reusable class for Gmail API operations.

Consolidates token management, MIME construction, and email sending
into a single service class for use across the application.
"""

import base64
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from typing import Dict, Any, Optional

import requests
from sqlalchemy.orm import Session

from job_outreach_tool.database.models import EmailAccount
from job_outreach_tool.core.config import settings
from job_outreach_tool.core.logger import get_logger

logger = get_logger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


class GmailService:
    """Handles Gmail API operations with automatic token refresh."""

    def __init__(self, db: Session):
        self.db = db

    # ── Token Management ─────────────────────────────────────────────────

    def refresh_token_if_needed(self, account: EmailAccount) -> str:
        """Return a valid access token, refreshing automatically if expired.

        Args:
            account: The EmailAccount with stored tokens.

        Returns:
            A valid access_token string.

        Raises:
            RuntimeError: If token is expired and refresh fails.
        """
        if account.token_expiry and account.token_expiry > datetime.utcnow():
            logger.debug("Access token still valid for %s", account.email_address)
            return account.access_token

        if not account.refresh_token:
            raise RuntimeError(
                f"Access token expired for {account.email_address} "
                f"and no refresh token is available. Please re-connect the account."
            )

        logger.info("Refreshing expired access token for %s", account.email_address)

        data = {
            "client_id": settings.GMAIL_CLIENT_ID,
            "client_secret": settings.GMAIL_CLIENT_SECRET,
            "refresh_token": account.refresh_token,
            "grant_type": "refresh_token",
        }

        resp = requests.post(GOOGLE_TOKEN_URL, data=data)
        if not resp.ok:
            logger.error("Token refresh failed for %s: %s", account.email_address, resp.text)
            raise RuntimeError(f"Token refresh failed: {resp.text}")

        result = resp.json()
        account.access_token = result["access_token"]
        account.token_expiry = datetime.utcnow() + timedelta(
            seconds=result.get("expires_in", 3600)
        )
        self.db.commit()

        logger.info("Token refreshed successfully for %s", account.email_address)
        return account.access_token

    # ── MIME Construction ────────────────────────────────────────────────

    @staticmethod
    def build_mime_message(
        to_email: str,
        subject: str,
        body: str,
        from_email: Optional[str] = None,
    ) -> str:
        """Construct a MIME message and return its base64url-encoded form.

        Args:
            to_email: Recipient address.
            subject: Email subject.
            body: Plain-text body.
            from_email: Optional sender address.

        Returns:
            URL-safe base64-encoded raw message string.
        """
        message = MIMEText(body)
        message["to"] = to_email
        message["subject"] = subject
        if from_email:
            message["from"] = from_email

        return base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

    # ── Email Sending ────────────────────────────────────────────────────

    def send_email(
        self,
        account: EmailAccount,
        to_email: str,
        subject: str,
        body: str,
    ) -> Dict[str, Any]:
        """Send an email via the Gmail API with automatic token refresh.

        Args:
            account: The EmailAccount to send from.
            to_email: Recipient email address.
            subject: Email subject line.
            body: Plain-text email body.

        Returns:
            Gmail API response dict (id, threadId, labelIds).

        Raises:
            RuntimeError: If the Gmail API call fails.
        """
        logger.info(
            "Sending email: from=%s, to=%s, subject=%s",
            account.email_address, to_email, subject,
        )

        # 1. Ensure valid token
        access_token = self.refresh_token_if_needed(account)

        # 2. Build MIME message
        raw_message = self.build_mime_message(
            to_email=to_email,
            subject=subject,
            body=body,
            from_email=account.email_address,
        )

        # 3. Send via Gmail API
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        payload = {"raw": raw_message}

        resp = requests.post(GMAIL_SEND_URL, json=payload, headers=headers)

        logger.info("Gmail API response: status=%d", resp.status_code)

        if not resp.ok:
            logger.error("Gmail send failed: %d %s", resp.status_code, resp.text)
            raise RuntimeError(f"Gmail API error: {resp.text}")

        result = resp.json()
        logger.info(
            "Email sent successfully: gmail_id=%s, thread_id=%s",
            result.get("id"), result.get("threadId"),
        )

        return result
