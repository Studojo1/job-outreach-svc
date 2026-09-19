"""Meta Conversions API — authoritative server-side Purchase.

The browser pixel reports Purchase too, but ad blockers and iOS delete a large
share of it, and a user who closes the tab straight after paying never reports
at all. Since campaigns can be set to bid on Purchase, a missed one is not just
a reporting gap, it is a lost training signal on the rarest event in the funnel.

This module is the trustworthy copy. It runs after the payment is committed and
reads the amount from our own PaymentOrder row, so the revenue it reports cannot
be influenced by anything the client sends. That is also why Purchase is
deliberately absent from the frontend's /api/meta-event allowlist: value-bearing
events must originate here.

Deduplication: event_id is the payment provider's own id, the same value the
browser sends, so Meta collapses the two copies into one conversion.
"""

import hashlib
import logging
import time

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

GRAPH_VERSION = "v21.0"


def _hash(value: str) -> str:
    """Meta requires sha256 of the trimmed, lowercased value."""
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


def is_configured() -> bool:
    return bool(settings.META_CAPI_TOKEN and settings.META_PIXEL_ID)


async def send_purchase(
    *,
    event_id: str,
    value: float,
    currency: str,
    email: str | None = None,
    external_id: str | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
    fbp: str | None = None,
    fbc: str | None = None,
) -> bool:
    """Report one confirmed purchase to Meta.

    Never raises: a payment must never fail because an analytics call did.
    Returns True only when Meta acknowledged the event.
    """
    if not is_configured():
        return False
    if not event_id:
        # Without a stable id the browser copy cannot be deduplicated against
        # this one, and the sale would be counted twice.
        logger.warning("[META_CAPI] Refusing to send Purchase with no event_id")
        return False

    user_data: dict = {}
    if email:
        user_data["em"] = [_hash(email)]
    if external_id:
        user_data["external_id"] = [_hash(str(external_id))]
    # Sent raw by design; Meta hashes or discards these itself.
    if client_ip:
        user_data["client_ip_address"] = client_ip
    if user_agent:
        user_data["client_user_agent"] = user_agent
    if fbp:
        user_data["fbp"] = fbp
    if fbc:
        user_data["fbc"] = fbc

    payload = {
        "data": [
            {
                "event_name": "Purchase",
                "event_time": int(time.time()),
                "event_id": event_id,
                "action_source": "website",
                "event_source_url": "https://studojo.com/outreach/enrichment",
                "user_data": user_data,
                "custom_data": {"value": round(float(value), 2), "currency": currency.upper()},
            }
        ]
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://graph.facebook.com/{GRAPH_VERSION}/{settings.META_PIXEL_ID}/events",
                params={"access_token": settings.META_CAPI_TOKEN},
                json=payload,
            )
        if resp.status_code != 200:
            # Log the body, not just the status: Meta puts the real reason
            # (bad token, malformed user_data) in the response.
            logger.error(
                "[META_CAPI] Purchase rejected %s for event_id=%s: %s",
                resp.status_code, event_id, resp.text[:300],
            )
            return False
        logger.info(
            "[META_CAPI] Purchase sent: event_id=%s value=%s %s", event_id, value, currency
        )
        return True
    except Exception as e:
        logger.warning("[META_CAPI] Purchase send failed for event_id=%s: %s", event_id, e)
        return False
