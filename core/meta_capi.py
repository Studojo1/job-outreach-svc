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
    # Test-mode payments are not revenue. Staging runs Razorpay in test mode and
    # its ₹90 / ₹1 test orders were landing in the live ad dataset, because the
    # token was the live one (audit ST-N07). Even if a token reaches staging
    # again, nothing is reported from there.
    if settings.RAZORPAY_TEST_MODE:
        return False
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


# ── Consent (audit HP-N13) ───────────────────────────────────────────────────
# EU/UK visitors must accept tracking before anything reaches Meta. The browser
# pixel is held by the frontend consent gate; the server-side copies (this
# module's Purchase, the frontend's /api/meta-event) are held here, from the
# consent state and device time zone the browser sends with the order.
#
# Mirrors consentRegion() in the frontend's app/lib/consent.ts.
_EXTRA_ZONES = {
    "Atlantic/Canary", "Atlantic/Madeira", "Atlantic/Azores", "Atlantic/Reykjavik",
    "Atlantic/Faroe", "Atlantic/Faeroe", "Arctic/Longyearbyen",
    "GB", "GB-Eire", "Eire", "Portugal", "Iceland", "Poland", "WET", "CET", "MET", "EET",
}

# EU, EEA and UK, for requests that carry no time zone (older clients).
CONSENT_COUNTRIES = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU",
    "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES",
    "SE", "IS", "LI", "NO", "GB", "GI",
}

# Stored in PaymentOrder.meta_fbp when the buyer has not consented, so the
# Purchase reported later (often from a webhook, with no browser behind it)
# knows to stay silent. A real _fbp always starts "fb.", so this cannot clash.
NO_CONSENT_MARK = "no-consent"


def consent_region(time_zone: str | None) -> bool:
    """Does a visitor in this time zone need to opt in? Unknown counts as yes."""
    if not time_zone:
        return True
    return time_zone.startswith("Europe/") or time_zone in _EXTRA_ZONES


def meta_allowed(consent: str | None, time_zone: str | None, country: str | None) -> bool:
    """May this buyer's conversion be reported to Meta?

    An explicit choice wins. With none, a visitor outside the EU/UK is fine
    and one inside it (by time zone, or by IP country when the client sent no
    time zone) is a no. Nothing known at all is a no: the safe side.
    """
    if consent == "denied":
        return False
    if consent == "granted":
        return True
    if time_zone:
        return not consent_region(time_zone)
    c = (country or "").upper()
    return bool(c) and c != "UNKNOWN" and c not in CONSENT_COUNTRIES
