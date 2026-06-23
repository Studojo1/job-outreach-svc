"""Login-free LinkedIn lead discovery via public web search (DuckDuckGo x-ray).

Finds public linkedin.com/in/ profiles matching a role + location WITHOUT a
LinkedIn session and WITHOUT spending Apollo credits. This lets a campaign show
leads BEFORE the user connects their LinkedIn account — the session is only
needed later, at send-time, to fire connection requests / messages.

Returns the same dict shape as automation_service.search_linkedin_leads:
    {name, headline, company, profile_url, profile_image_url}
"""

import asyncio
import logging
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from core.config import settings

logger = logging.getLogger(__name__)

# DuckDuckGo's "lite" endpoint is far less aggressively bot-challenged than the
# main html endpoint. Routed through the residential proxy (rotating IPs) it
# returns real linkedin.com/in/ results reliably; the cluster's own egress IP
# gets rate-limited (HTTP 202) within a few queries.
_DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"
_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_HTTP_TIMEOUT = 20.0


async def _ddg_lite_search(query: str) -> list[dict]:
    """POST to the DDG lite endpoint (via residential proxy) and return
    [{title, url, snippet}] for linkedin.com/in/ results. Never raises."""
    proxy = (getattr(settings, "LINKEDIN_PROXY_URL", "") or "").strip() or None
    try:
        async with httpx.AsyncClient(
            headers=_HTTP_HEADERS, follow_redirects=True,
            timeout=_HTTP_TIMEOUT, verify=False, proxy=proxy,
        ) as client:
            resp = await client.post(_DDG_LITE_URL, data={"q": query})
        if resp.status_code != 200:
            logger.warning("[WebDiscovery] DDG lite non-200 (%d) for %r", resp.status_code, query[:60])
            return []
        soup = BeautifulSoup(resp.text, "lxml")
        out = []
        for a in soup.select("a"):
            href = a.get("href") or ""
            if "linkedin.com/in/" not in href:
                continue
            out.append({"title": a.get_text(strip=True), "url": href, "snippet": ""})
        return out
    except Exception as exc:
        logger.warning("[WebDiscovery] DDG lite failed for %r: %s", query[:60], exc)
        return []

# Trailing LinkedIn page-title suffixes to strip before parsing the name/headline.
_SUFFIX_RE = re.compile(
    r"\s*[-|]\s*LinkedIn(\s+\w+)?\s*$", re.IGNORECASE
)
# Tokens that mark a segment as a job title rather than a person's name.
_TITLE_HINTS = (
    "manager", "intern", "head", "lead", "director", "officer", "executive",
    "specialist", "analyst", "associate", "consultant", "engineer", "founder",
    "ceo", "cmo", "marketing", "growth", "brand", "content", "strategist",
    "at ", "@",
)
# Delay between DDG queries — the html endpoint returns 202 if hit too fast.
_QUERY_DELAY_S = 2.5
_MAX_202_RETRIES = 2


def _clean_title(raw: str) -> str:
    return _SUFFIX_RE.sub("", raw or "").strip()


def _looks_like_title(segment: str) -> bool:
    s = (segment or "").lower()
    if any(h in s for h in _TITLE_HINTS):
        return True
    # A real name is usually 1-4 short words; long segments are headlines.
    return len(segment.split()) > 4


def _name_from_slug(url: str) -> str:
    """Derive a display name from a /in/<slug> URL when the title has none.

    'richa-sharma-ab342968' -> 'Richa Sharma' (drops trailing id-ish tokens).
    """
    try:
        path = urlparse(url).path.rstrip("/")
        slug = path.split("/in/")[-1].split("/")[0]
    except Exception:
        return ""
    parts = slug.split("-")
    words = []
    for p in parts:
        # Drop tokens that contain digits or are clearly random ids.
        if any(ch.isdigit() for ch in p) or len(p) > 14:
            break
        if p.isalpha():
            words.append(p.capitalize())
    return " ".join(words[:3]).strip()


def _parse_result(item: dict) -> dict | None:
    url = (item.get("url") or "").split("?")[0]
    if "linkedin.com/in/" not in url:
        return None

    title = _clean_title(item.get("title", ""))
    snippet = item.get("snippet", "") or ""

    name, headline, company = "", "", None
    segments = [s.strip() for s in re.split(r"\s+[-–|]\s+", title) if s.strip()]

    if segments and not _looks_like_title(segments[0]):
        name = segments[0]
        headline = " - ".join(segments[1:]).strip()
    else:
        # Title leads with a job title (no name) — derive name from the URL slug.
        name = _name_from_slug(url)
        headline = title

    # Pull company out of an "at <Company>" pattern in the headline.
    m = re.search(r"\bat\s+(.+)$", headline, re.IGNORECASE)
    if m:
        company = m.group(1).strip()
    if company:
        # Strip trailing ellipsis / page-title noise left by truncated snippets.
        company = re.sub(r"\s*\.{2,}\s*$", "", company).strip(" .|-") or None

    if not headline and snippet:
        headline = snippet[:160]

    # Drop obvious non-person / unusable entries (company pages, generic labels).
    nm = (name or "").strip()
    if nm.lower() in {"", "linkedin member", "marketing team"} or "team" in nm.lower():
        return None
    if len(nm.split()) < 2 or any(h in nm.lower() for h in _TITLE_HINTS):
        return None

    return {
        "name": nm,
        "headline": headline or None,
        "company": company,
        "profile_url": url,
        "profile_image_url": None,
    }


def _profile_key(url: str) -> str:
    try:
        return urlparse(url).path.rstrip("/").split("/in/")[-1].split("/")[0].lower()
    except Exception:
        return url.lower()


# Major cities per country — DDG lite has no page offset, so the only way to
# pull fresh profiles is genuinely different queries. Expanding a country into
# its hub cities multiplies distinct results without spending extra sources.
_COUNTRY_CITIES = {
    "india": ["Bangalore", "Mumbai", "Delhi", "Gurgaon", "Hyderabad", "Pune", "Chennai"],
    "united states": ["New York", "San Francisco", "Los Angeles", "Chicago", "Austin"],
    "usa": ["New York", "San Francisco", "Los Angeles", "Chicago", "Austin"],
    "us": ["New York", "San Francisco", "Los Angeles", "Chicago", "Austin"],
    "united kingdom": ["London", "Manchester", "Birmingham", "Edinburgh"],
    "uk": ["London", "Manchester", "Birmingham", "Edinburgh"],
    "uae": ["Dubai", "Abu Dhabi", "Sharjah"],
    "united arab emirates": ["Dubai", "Abu Dhabi", "Sharjah"],
    "singapore": ["Singapore"],
}


def _expand_locations(locations: list[str]) -> list[str]:
    out: list[str] = []
    for loc in locations:
        l = loc.strip()
        if not l:
            continue
        cities = _COUNTRY_CITIES.get(l.lower())
        if cities:
            out.append(l)            # keep the country-level query too
            out.extend(cities)
        else:
            out.append(l)            # already a city / region
    return out or [""]


def _build_queries(target_role: str, locations: list[str], keywords: str | None) -> list[str]:
    """Diverse x-ray queries. DDG lite has no page offset, so query variety
    (role x each city x keyword) is how we reach volume."""
    role = (target_role or "").strip() or "Marketing Manager"
    locs = _expand_locations([l for l in (locations or []) if l and l.strip()])
    kw = (keywords or "").strip()

    queries: list[str] = []
    for loc in locs:
        base = f'site:linkedin.com/in "{role}"'
        queries.append(f'{base} {loc}'.strip())
        if loc:
            queries.append(f'"{role}" "{loc}" site:linkedin.com/in')
        if kw:
            queries.append(f'{base} "{kw}" {loc}'.strip())
    queries.append(f'site:linkedin.com/in "{role}"')  # broad fallback
    # De-dupe while preserving order.
    seen, out = set(), []
    for q in queries:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out


async def _search_with_retry(query: str) -> list[dict]:
    """DDG lite search with backoff. Each retry goes out on a fresh rotating
    residential proxy IP, so a transient 202 usually clears on retry."""
    for attempt in range(_MAX_202_RETRIES + 1):
        res = await _ddg_lite_search(query)
        if res:
            return res
        if attempt < _MAX_202_RETRIES:
            await asyncio.sleep(_QUERY_DELAY_S * (attempt + 1))
    return []


async def discover_leads_via_search(
    target_role: str,
    locations: list[str] | None = None,
    industries: list[str] | None = None,
    keywords: str | None = None,
    limit: int = 30,
) -> list[dict]:
    """Find public LinkedIn profiles matching the ICP — no login, no Apollo."""
    queries = _build_queries(target_role, locations or [], keywords)
    logger.info(
        "[WebDiscovery] role=%r locations=%r -> %d queries (target %d leads)",
        target_role, locations, len(queries), limit,
    )

    leads: list[dict] = []
    seen: set[str] = set()

    for i, q in enumerate(queries):
        if len(leads) >= limit:
            break
        results = await _search_with_retry(q)
        for item in results:
            parsed = _parse_result(item)
            if not parsed:
                continue
            key = _profile_key(parsed["profile_url"])
            if not key or key in seen:
                continue
            seen.add(key)
            leads.append(parsed)
            if len(leads) >= limit:
                break
        # Be polite to DDG between queries.
        if i < len(queries) - 1:
            await asyncio.sleep(_QUERY_DELAY_S)

    logger.info("[WebDiscovery] role=%r found %d unique leads", target_role, len(leads))
    return leads[:limit]
