"""IP geolocation for matching a customer's residential proxy exit to where
their LinkedIn account actually logs in from (account-safety).

When a customer connects their LinkedIn account, we geolocate the real IP their
browser is on and pin the Evomi proxy exit to that country. A LinkedIn account
whose automated activity comes from its usual country looks far less suspicious
than one suddenly logging in from a random datacenter/foreign residential IP.
"""

import logging

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 6.0


def client_ip_from_headers(x_forwarded_for: str | None, x_real_ip: str | None,
                           client_host: str | None) -> str | None:
    """Best-effort real client IP behind the ingress. X-Forwarded-For is a
    comma-separated chain (client first); take the first public-looking hop."""
    if x_forwarded_for:
        for part in x_forwarded_for.split(","):
            ip = part.strip()
            if ip and not _is_private(ip):
                return ip
    if x_real_ip and not _is_private(x_real_ip):
        return x_real_ip
    return client_host


def _is_private(ip: str) -> bool:
    return (
        ip.startswith(("10.", "192.168.", "127.", "169.254.", "::1", "fc", "fd"))
        or any(ip.startswith(f"172.{n}.") for n in range(16, 32))
    )


async def geolocate(ip: str | None) -> tuple[str | None, str | None]:
    """Return (country_code ISO-2, city) for an IP, or (None, None). Never raises.

    Tries ip-api.com (free, no key) then ipinfo.io as a fallback.
    """
    if not ip or _is_private(ip):
        return None, None
    # Primary: ip-api.com
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"http://ip-api.com/json/{ip}?fields=status,countryCode,city")
            if r.status_code == 200:
                d = r.json()
                if d.get("status") == "success" and d.get("countryCode"):
                    return d["countryCode"].upper(), (d.get("city") or None)
    except Exception as e:
        logger.debug("geolocate ip-api failed for %s: %s", ip, e)
    # Fallback: ipinfo.io
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"https://ipinfo.io/{ip}/json")
            if r.status_code == 200:
                d = r.json()
                if d.get("country"):
                    return d["country"].upper(), (d.get("city") or None)
    except Exception as e:
        logger.debug("geolocate ipinfo failed for %s: %s", ip, e)
    return None, None
