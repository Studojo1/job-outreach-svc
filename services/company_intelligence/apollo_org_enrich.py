"""Apollo /organizations/enrich wrapper.

Returns Apollo's full company record for a given domain — keywords, technologies,
industries, employee_count, founded_year, headquarters, etc. Uses the same
APOLLO_API_KEY as the people search; org enrichment is free under the current plan.
"""

import time
import requests
from typing import Dict, Any, Optional

from core.config import settings
from core.logger import get_logger

logger = get_logger(__name__)

APOLLO_BASE_URL = "https://api.apollo.io/api/v1"
ENRICH_TIMEOUT_SEC = 12


def enrich_organization(
    domain: str | None = None,
    name: str | None = None,
) -> Optional[Dict[str, Any]]:
    """Call Apollo /organizations/enrich. Looks up by domain when given;
    otherwise falls back to a name-based lookup via /mixed_companies/api_search.

    Apollo's /people/search returns extremely thin organization data on most
    plans (just the name + a few has_* booleans), so for the vast majority
    of leads we discover we have NO domain — only the company name. The
    name-based fallback uses /mixed_companies/api_search to resolve a name
    to an org record, then returns the same parsed dict shape.

    Returns the parsed organization dict on success, or None on any failure.
    Failures are logged but never raised — the caller falls back to whatever
    partial data exists.
    """
    if not domain and not name:
        return None
    if not settings.APOLLO_API_KEY or settings.APOLLO_API_KEY == "your_apollo_key_here":
        logger.warning("[APOLLO_ENRICH] APOLLO_API_KEY missing, skipping enrich")
        return None

    headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": settings.APOLLO_API_KEY,
    }
    label = domain or name

    # Same throttle as people search to stay under Apollo's per-second limits.
    time.sleep(0.2)

    org: Optional[Dict[str, Any]] = None
    try:
        if domain:
            url = f"{APOLLO_BASE_URL}/organizations/enrich"
            resp = requests.get(url, params={"domain": domain}, headers=headers, timeout=ENRICH_TIMEOUT_SEC)
            if resp.ok:
                org = (resp.json() or {}).get("organization") or None
            else:
                logger.info("[APOLLO_ENRICH] domain lookup HTTP %d for %s", resp.status_code, label)
        if not org and name:
            # Fallback: name-based lookup via mixed_companies/api_search.
            url = f"{APOLLO_BASE_URL}/mixed_companies/search"
            payload = {"q_organization_name": name, "page": 1, "per_page": 1}
            resp = requests.post(url, json=payload, headers=headers, timeout=ENRICH_TIMEOUT_SEC)
            if resp.ok:
                body = resp.json() or {}
                orgs = body.get("organizations") or body.get("accounts") or []
                if orgs and isinstance(orgs[0], dict):
                    org = orgs[0]
            else:
                logger.info("[APOLLO_ENRICH] name lookup HTTP %d for %s", resp.status_code, label)
    except requests.RequestException as e:
        logger.warning("[APOLLO_ENRICH] network error for %s: %s", label, e)
        return None
    except ValueError:
        logger.warning("[APOLLO_ENRICH] non-JSON response for %s", label)
        return None

    if not org:
        logger.info("[APOLLO_ENRICH] no organization found for %s", label)
        return None

    # Apollo's response uses different key names for the website depending on
    # which endpoint we hit. Normalize them all into one.
    discovered_domain = (
        _domainify(org.get("primary_domain"))
        or _domainify(org.get("website_url"))
        or _domainify(org.get("domain"))
        or _domainify(org.get("primary_domain_in_lower_case"))
    )

    parsed = {
        "apollo_org_id": org.get("id"),
        "name": org.get("name"),
        "discovered_domain": discovered_domain or domain,  # propagate so caller can persist it
        "short_description": (org.get("short_description") or org.get("seo_description") or "")[:2000] or None,
        "industries": _coerce_list(org.get("industries")) or _coerce_list([org.get("industry")]),
        "keywords": _coerce_list(org.get("keywords")),
        "technologies": _extract_technologies(org),
        "employee_count": _coerce_int(org.get("estimated_num_employees") or org.get("employee_count")),
        "founded_year": _coerce_int(org.get("founded_year")),
        "headquarters_city": org.get("city") or org.get("primary_city"),
        # Round-2 momentum signals — were previously discarded.
        "recent_news_summary": _extract_news_summary(org),
        "latest_funding_round_date": _parse_date(org.get("latest_funding_round_date")),
        "latest_funding_amount": _coerce_int(org.get("latest_funding_round_amount") or org.get("latest_funding_amount")),
        "linkedin_url": org.get("linkedin_url"),
    }

    logger.info(
        "[APOLLO_ENRICH] OK domain=%s name=%s industries=%d keywords=%d tech=%d employees=%s funding=%s news=%s",
        domain,
        parsed["name"],
        len(parsed["industries"] or []),
        len(parsed["keywords"] or []),
        len(parsed["technologies"] or []),
        parsed["employee_count"],
        parsed["latest_funding_round_date"],
        bool(parsed["recent_news_summary"]),
    )
    return parsed


def _domainify(value: Any) -> Optional[str]:
    """Normalize any URL-ish string into a clean lowercase domain."""
    if not value or not isinstance(value, str):
        return None
    raw = value.strip().lower()
    if raw.startswith(("http://", "https://")):
        raw = raw.split("://", 1)[1]
    raw = raw.split("/", 1)[0]
    if raw.startswith("www."):
        raw = raw[4:]
    return raw or None


def _parse_date(value: Any) -> Optional[str]:
    """Return ISO yyyy-mm-dd string if Apollo gave us a parseable date.
    Apollo returns dates as ISO strings already; we just normalize."""
    if not value:
        return None
    if isinstance(value, str):
        return value[:10]  # 'yyyy-mm-dd' or 'yyyy-mm-ddThh...'
    return None


def _extract_news_summary(org: Dict[str, Any]) -> Optional[str]:
    """Apollo exposes recent news in a few possible fields depending on plan.
    Try `recent_news_summary` first, then fall back to concatenating titles
    from a `current_news` array if present. Cap to 800 chars."""
    direct = org.get("recent_news_summary")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()[:800]
    news = org.get("current_news") or org.get("news") or []
    if isinstance(news, list) and news:
        titles = []
        for item in news[:5]:
            if isinstance(item, dict):
                t = item.get("title") or item.get("headline")
                if t:
                    titles.append(str(t).strip())
            elif item:
                titles.append(str(item).strip())
        joined = " | ".join(titles)
        return joined[:800] if joined else None
    return None


def _coerce_list(value: Any) -> Optional[list]:
    if not value:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        cleaned = [str(v).strip() for v in value if v is not None and str(v).strip()]
        return cleaned or None
    return None


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_technologies(org: Dict[str, Any]) -> Optional[list]:
    """Apollo returns technologies as a list of dicts with `name` keys, or as
    `current_technologies` array of strings depending on plan. Handle both."""
    tech = org.get("current_technologies") or org.get("technologies") or []
    if not tech:
        return None
    out = []
    for t in tech:
        if isinstance(t, dict):
            name = t.get("name") or t.get("uid")
            if name:
                out.append(str(name).strip())
        elif t:
            out.append(str(t).strip())
    return out or None
