"""Normalising company domains before they are stored on a lead."""
import re
from typing import Optional

_DOMAIN_RE = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}")


def clean_domain(value) -> Optional[str]:
    """A bare hostname, or None.

    The company-research step sometimes returns the domain wrapped in prose or
    markdown ("neurofin.ai. ([neurofin.ai](https://neurofin.ai/))"). About 7% of
    stored domains looked like that, and every one of them broke the card's logo.
    """
    if not value or not isinstance(value, str):
        return None
    text = re.sub(r"^[a-z]+://", "", value.strip().lower())
    m = _DOMAIN_RE.search(text)
    if not m:
        return None
    host = m.group(0)
    return host[4:] if host.startswith("www.") else host
