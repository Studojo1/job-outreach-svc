"""Per-operation residential-proxy country targeting.

The customer's exit country is decided once (geolocated from their real IP at
connect, stored on the LinkedInToken) and then needs to apply to every proxied
request for that account — login, Voyager calls, and the Playwright send. Rather
than thread a `country` argument through ~10 functions, we stash it in a
context variable that the low-level proxy builders read. The daemon sets it per
campaign tick (sequential), and the login endpoint sets it before logging in.
contextvars are async-task-local, so this is safe even if calls interleave.
"""

import re as _re
from contextvars import ContextVar

# ISO-2 country code (e.g. "IN", "FR") for the current operation, or None.
proxy_country_var: ContextVar = ContextVar("li_proxy_country", default=None)


def apply_country(proxy_url: str, country_code: str | None) -> str:
    """Inject Evomi country targeting (`_country-XX`) into the password portion
    of a standard `http://user:pass@host:port` proxy URL. No-op when there's no
    country, the URL isn't in @-form, or a country modifier is already present.
    """
    if not country_code or not proxy_url:
        return proxy_url
    m = _re.match(r"^(http://[^:]+):([^@]+)@(.+)$", proxy_url)
    if not m:
        return proxy_url
    scheme_user, password, host_port = m.group(1), m.group(2), m.group(3)
    if "_country-" in password:
        return proxy_url
    return f"{scheme_user}:{password}_country-{country_code.upper()}@{host_port}"


def apply_current_country(proxy_url: str) -> str:
    """Apply the country from the current context (set by the daemon/login)."""
    return apply_country(proxy_url, proxy_country_var.get())
