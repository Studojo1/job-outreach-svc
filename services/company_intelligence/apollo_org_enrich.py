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


def enrich_organization(domain: str) -> Optional[Dict[str, Any]]:
    """Call Apollo /organizations/enrich for a domain.

    Returns the parsed organization dict on success, or None on any failure
    (rate limit, 4xx/5xx, network error, missing data). Failures are logged
    but never raised — the caller is expected to fall back to Lead.company_description.
    """
    if not domain:
        return None
    if not settings.APOLLO_API_KEY or settings.APOLLO_API_KEY == "your_apollo_key_here":
        logger.warning("[APOLLO_ENRICH] APOLLO_API_KEY missing, skipping enrich for %s", domain)
        return None

    url = f"{APOLLO_BASE_URL}/organizations/enrich"
    headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": settings.APOLLO_API_KEY,
    }
    params = {"domain": domain}

    # Same throttle as people search to stay under Apollo's per-second limits.
    time.sleep(0.2)

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=ENRICH_TIMEOUT_SEC)
    except requests.RequestException as e:
        logger.warning("[APOLLO_ENRICH] network error for %s: %s", domain, e)
        return None

    if not resp.ok:
        logger.warning("[APOLLO_ENRICH] HTTP %d for %s: %s", resp.status_code, domain, resp.text[:200])
        return None

    try:
        body = resp.json()
    except ValueError:
        logger.warning("[APOLLO_ENRICH] non-JSON response for %s", domain)
        return None

    org = body.get("organization") or {}
    if not org:
        logger.info("[APOLLO_ENRICH] empty organization for %s", domain)
        return None

    parsed = {
        "apollo_org_id": org.get("id"),
        "name": org.get("name"),
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
