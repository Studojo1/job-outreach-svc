"""Geo-detection utility — determines user country from request IP.

Uses a lightweight HTTP GeoIP lookup with in-memory caching.
Falls back to 'UNKNOWN' on any error (treated as international).
"""

import logging
from functools import lru_cache

import httpx
from fastapi import Request

logger = logging.getLogger(__name__)

# Cache up to 4096 IP lookups in memory (covers typical daily unique visitors)
@lru_cache(maxsize=4096)
def _lookup_country(ip: str) -> str:
    """Look up country code for an IP address using ip-api.com (free, no key needed)."""
    try:
        resp = httpx.get(
            f"http://ip-api.com/json/{ip}",
            params={"fields": "countryCode,status"},
            timeout=3.0,
        )
        data = resp.json()
        if data.get("status") == "success":
            return data.get("countryCode", "UNKNOWN")
    except Exception as e:
        logger.warning("[GEO] IP lookup failed for %s: %s", ip, e)
    return "UNKNOWN"


def get_client_ip(request: Request) -> str:
    """Extract the real client IP from X-Forwarded-For or fallback to direct connection."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        # First IP in the chain is the original client
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


def detect_country(request: Request) -> str:
    """Return ISO 3166-1 alpha-2 country code (e.g. 'IN', 'US'). Defaults to 'UNKNOWN'."""
    ip = get_client_ip(request)
    # Skip lookups for localhost/private IPs
    if ip.startswith(("127.", "10.", "172.", "192.168.", "0.0.0.0", "::1")):
        return "UNKNOWN"
    return _lookup_country(ip)


def is_india(request: Request) -> bool:
    """Returns True if the request originates from India."""
    return detect_country(request) == "IN"