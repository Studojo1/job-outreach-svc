"""Apollo /organizations/enrich wrapper.

Returns Apollo's full company record for a given domain — keywords, technologies,
industries, employee_count, founded_year, headquarters, etc. Uses the same
APOLLO_API_KEY as the people search; org enrichment is free under the current plan.
"""

import re
import time
from typing import Dict, Any, List, Optional

from core.logger import get_logger
from services.shared.apollo_key_manager import apollo_get, apollo_keys

logger = get_logger(__name__)

APOLLO_BASE_URL = "https://api.apollo.io/api/v1"
ENRICH_TIMEOUT_SEC = 12


def enrich_organization(
    domain: str | None = None,
    name: str | None = None,
) -> Optional[Dict[str, Any]]:
    """Call Apollo /organizations/enrich. Always uses domain-based lookup.

    When only a name is provided, we generate up to 5 candidate domains by
    normalizing the name (e.g. "ConnectWise" → "connectwise.com",
    "InStore.ai" → "instore.ai", "BI Worldwide (India)" → "biworldwide.com")
    and try each in turn. The first that returns rich data wins.

    We cannot use Apollo's /mixed_companies/search for name-based discovery
    because on our plan it returns empty org records (verified via live test
    on 48 companies — every result had industries=0, keywords=0, tech=0).

    Returns the parsed organization dict on success, or None on any failure.
    Failures are logged but never raised.
    """
    if not domain and not name:
        return None
    if not apollo_keys.has_valid_key():
        logger.warning("[APOLLO_ENRICH] No valid Apollo API key, skipping enrich")
        return None

    label = domain or name

    # Build the list of candidate domains to try, in priority order.
    candidates: List[str] = []
    if domain:
        candidates.append(_domainify(domain) or domain)
    if name:
        for guess in _guess_domains_from_name(name):
            if guess and guess not in candidates:
                candidates.append(guess)

    # Try each candidate; first one that returns a non-empty org wins.
    org: Optional[Dict[str, Any]] = None
    successful_domain: Optional[str] = None
    for candidate in candidates[:4]:  # cap at 4 attempts so we don't waste 5x time
        # Same throttle as people search to stay under Apollo's per-second limits.
        time.sleep(0.2)
        try:
            url = f"{APOLLO_BASE_URL}/organizations/enrich"
            resp = apollo_get(url, params={"domain": candidate}, timeout=ENRICH_TIMEOUT_SEC)
        except Exception as e:
            logger.warning("[APOLLO_ENRICH] network error for %s (try %s): %s", label, candidate, e)
            continue
        if not resp.ok:
            logger.debug("[APOLLO_ENRICH] HTTP %d for %s (try %s)", resp.status_code, label, candidate)
            continue
        try:
            candidate_org = (resp.json() or {}).get("organization") or None
        except ValueError:
            continue
        if not candidate_org:
            continue
        # Accept only if Apollo returned something genuinely useful — at least
        # an industry, an employee count, or a description. Empty stub records
        # would just produce generic justifier output again.
        has_signal = bool(
            candidate_org.get("industry")
            or candidate_org.get("industries")
            or candidate_org.get("estimated_num_employees")
            or candidate_org.get("short_description")
            or candidate_org.get("keywords")
        )
        if has_signal:
            org = candidate_org
            successful_domain = candidate
            break

    if not org:
        logger.info("[APOLLO_ENRICH] no rich data found for %s (tried %d candidates)", label, len(candidates[:4]))
        return None
    if successful_domain and successful_domain != domain:
        logger.info("[APOLLO_ENRICH] resolved %s → domain %s", label, successful_domain)

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


# Common suffixes / qualifiers that don't belong in a domain guess.
# Stripped before generating candidate domains.
_NAME_NOISE_PATTERNS = [
    r"\(.*?\)",                                  # "(India)", "(formerly X)"
    r"\b(pvt|pvt\.|private)\s*(ltd|ltd\.|limited)\b",
    r"\b(india|usa|uk|inc|inc\.|llc|llp|corp|corporation|company|co\.)\b",
    r"\b(software|technologies|technology|tech|systems|labs|solutions|services|center|centre)\b",
]


def _guess_domains_from_name(name: str) -> List[str]:
    """Generate up to ~5 candidate domains from a company name.

    Examples:
      "ConnectWise"            → ["connectwise.com", "connectwise.io", "connectwise.ai"]
      "InStore.ai"             → ["instore.ai", "instore.com"]
      "BI Worldwide (India)"   → ["biworldwide.com", "bi.com", ...]
      "Sony India Software"    → ["sony.com", "sonyindia.com"]
    """
    if not name or not isinstance(name, str):
        return []

    cleaned = name.strip().lower()
    # If the name itself looks like a domain, try it directly first.
    if "." in cleaned and " " not in cleaned and len(cleaned) <= 30:
        d = _domainify(cleaned)
        if d:
            return [d, d.rsplit(".", 1)[0] + ".com"]

    # Strip noise patterns.
    for pattern in _NAME_NOISE_PATTERNS:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[^a-z0-9 ]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if not cleaned:
        return []

    words = cleaned.split()
    candidates: List[str] = []

    # 1. All words concatenated: "biworldwide", "sonyindiasoftware".
    base = "".join(words)
    if 3 <= len(base) <= 30:
        candidates.extend([f"{base}.com", f"{base}.ai", f"{base}.io"])

    # 2. Just the first word: helps for "Sony India Software" → "sony.com".
    if len(words) > 1:
        first = words[0]
        if 3 <= len(first) <= 20:
            candidates.append(f"{first}.com")

    # 3. First two words concatenated: "biworldwide" again, but also
    #    "sonyindia" for cases where that's the actual brand.
    if len(words) >= 2:
        first_two = words[0] + words[1]
        if 4 <= len(first_two) <= 30 and f"{first_two}.com" not in candidates:
            candidates.append(f"{first_two}.com")

    # Dedupe while preserving order
    seen: set = set()
    out: List[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


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
