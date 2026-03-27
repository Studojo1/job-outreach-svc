"""Gmail Inbox Service — Read inbox messages for reply detection and bounce tracking.

Uses gmail.readonly scope (already authorized in the OAuth flow) to poll for
replies and bounce notifications to sent outreach emails.
"""

import re
import base64
from typing import Dict, Any, List, Optional

import requests

from core.logger import get_logger

logger = get_logger(__name__)

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


def list_inbox_messages(
    access_token: str,
    after_epoch: int,
    max_results: int = 50,
) -> List[Dict[str, str]]:
    """List inbox messages received after a given epoch timestamp.

    Uses Gmail search query `in:inbox after:{epoch}` to only fetch new messages
    since the last check — avoids re-processing and keeps API calls minimal.

    Args:
        access_token: Valid Gmail OAuth access token with gmail.readonly scope.
        after_epoch: Unix epoch seconds — only return messages after this time.
        max_results: Max messages to return (default 50).

    Returns:
        List of dicts with keys: id, threadId.
        Empty list if no messages or API error.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {
        "q": f"in:inbox after:{after_epoch}",
        "maxResults": max_results,
    }

    try:
        resp = requests.get(
            f"{GMAIL_API_BASE}/messages",
            headers=headers,
            params=params,
            timeout=10,
        )
        if not resp.ok:
            logger.error("[INBOX] Gmail list messages failed: %d %s", resp.status_code, resp.text[:200])
            return []
        data = resp.json()
        messages = data.get("messages", [])
        logger.info("[INBOX] Found %d inbox messages after epoch %d", len(messages), after_epoch)
        return messages  # Each item: {id, threadId}
    except Exception as e:
        logger.error("[INBOX] Gmail list messages error: %s", e)
        return []


def get_message_detail(access_token: str, message_id: str) -> Optional[Dict[str, Any]]:
    """Fetch full message details including headers and body.

    Args:
        access_token: Valid Gmail OAuth access token.
        message_id: Gmail message ID.

    Returns:
        Dict with keys: message_id, thread_id, from_email, subject, body_text, internal_date.
        None if API error.
    """
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        resp = requests.get(
            f"{GMAIL_API_BASE}/messages/{message_id}",
            headers=headers,
            params={"format": "full"},
            timeout=10,
        )
        if not resp.ok:
            logger.error("[INBOX] Gmail get message failed: %d %s", resp.status_code, resp.text[:200])
            return None

        msg = resp.json()
        msg_headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }

        body_text = _extract_body_text(msg.get("payload", {}))

        return {
            "message_id": msg.get("id"),
            "thread_id": msg.get("threadId"),
            "from_email": msg_headers.get("from", ""),
            "subject": msg_headers.get("subject", ""),
            "body_text": body_text,
            "internal_date": int(msg.get("internalDate", 0)) // 1000,  # ms -> seconds
        }
    except Exception as e:
        logger.error("[INBOX] Gmail get message error: %s", e)
        return None


def _extract_body_text(payload: Dict) -> str:
    """Extract plain text body from Gmail message payload.

    Handles both simple (non-multipart) and multipart messages.
    Prefers text/plain; falls back to text/html with tag stripping.
    """
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data")

    # Simple non-multipart message
    if mime_type == "text/plain" and body_data:
        return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")

    # Multipart message — search parts
    parts = payload.get("parts", [])
    plain_text = ""
    html_text = ""

    for part in parts:
        part_mime = part.get("mimeType", "")
        part_data = part.get("body", {}).get("data")

        if part_mime == "text/plain" and part_data:
            plain_text = base64.urlsafe_b64decode(part_data).decode("utf-8", errors="replace")
        elif part_mime == "text/html" and part_data:
            html_text = base64.urlsafe_b64decode(part_data).decode("utf-8", errors="replace")

        # Recurse into nested multipart
        if part.get("parts"):
            nested = _extract_body_text(part)
            if nested and not plain_text:
                plain_text = nested

    if plain_text:
        return plain_text[:10000]

    # Fallback: strip HTML tags
    if html_text:
        clean = re.sub(r"<[^>]+>", " ", html_text)
        clean = re.sub(r"\s+", " ", clean).strip()
        return clean[:5000]

    return ""


def is_bounce_message(from_email: str) -> bool:
    """Check if an email is from a mail delivery system (bounce notification).

    Args:
        from_email: The From header value of the incoming message.

    Returns:
        True if the sender is a mail delivery / bounce notification system.
    """
    from_lower = from_email.lower()
    bounce_senders = [
        "mailer-daemon",
        "mail delivery subsystem",
        "postmaster",
    ]
    return any(sender in from_lower for sender in bounce_senders)


def extract_bounce_reason(body_text: str) -> str:
    """Extract a human-readable bounce reason from a bounce message body.

    Looks for common SMTP error patterns (5xx codes, delivery failure keywords).

    Args:
        body_text: The plain text body of the bounce message.

    Returns:
        A truncated reason string (max 500 chars), or "Unknown bounce reason".
    """
    patterns = [
        r"(5\d{2}[\s\-]+[^\n]{5,100})",            # 550 User unknown, 552 Mailbox full, etc.
        r"(Delivery to .+? has been suspended)",     # Gmail-specific suspension
        r"(The email account .+? does not exist)",   # Gmail non-existent address
        r"(Address rejected[^\n]*)",
        r"(User unknown[^\n]*)",
        r"(Mailbox full[^\n]*)",
        r"(DNS Error[^\n]*)",
        r"(Message rejected[^\n]*)",
        r"(does not exist[^\n]*)",
    ]

    for pattern in patterns:
        match = re.search(pattern, body_text, re.IGNORECASE)
        if match:
            return match.group(1).strip()[:500]

    # Fallback: find first line mentioning delivery failure
    for line in body_text.split("\n"):
        line = line.strip()
        if len(line) > 20 and any(
            kw in line.lower()
            for kw in ("delivery", "failed", "error", "undeliverable", "rejected")
        ):
            return line[:500]

    return "Unknown bounce reason"
