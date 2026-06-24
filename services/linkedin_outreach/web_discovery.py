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
_MAX_202_RETRIES = 4


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
    "india": ["Bangalore", "Mumbai", "Delhi", "Gurgaon", "Hyderabad", "Pune", "Chennai",
              "Kolkata", "Noida", "Ahmedabad", "Jaipur"],
    "united states": ["New York", "San Francisco", "Los Angeles", "Chicago", "Austin",
                      "Boston", "Seattle", "Atlanta", "Denver", "Miami"],
    "usa": ["New York", "San Francisco", "Los Angeles", "Chicago", "Austin",
            "Boston", "Seattle", "Atlanta", "Denver", "Miami"],
    "us": ["New York", "San Francisco", "Los Angeles", "Chicago", "Austin",
           "Boston", "Seattle", "Atlanta", "Denver", "Miami"],
    "united kingdom": ["London", "Manchester", "Birmingham", "Edinburgh", "Leeds", "Bristol", "Glasgow"],
    "uk": ["London", "Manchester", "Birmingham", "Edinburgh", "Leeds", "Bristol", "Glasgow"],
    "uae": ["Dubai", "Abu Dhabi", "Sharjah"],
    "united arab emirates": ["Dubai", "Abu Dhabi", "Sharjah"],
    "singapore": ["Singapore"],
    "france": ["Paris", "Lyon", "Toulouse", "Marseille", "Lille", "Bordeaux",
               "Nantes", "Nice", "Strasbourg", "Montpellier", "Rennes"],
    "germany": ["Berlin", "Munich", "Hamburg", "Frankfurt", "Cologne", "Stuttgart"],
    "spain": ["Madrid", "Barcelona", "Valencia", "Seville"],
    "italy": ["Milan", "Rome", "Turin", "Bologna"],
    "netherlands": ["Amsterdam", "Rotterdam", "The Hague", "Utrecht"],
    "canada": ["Toronto", "Vancouver", "Montreal", "Calgary", "Ottawa"],
    "australia": ["Sydney", "Melbourne", "Brisbane", "Perth"],
}

# Map a country (any common name/alias) to LinkedIn's country subdomain. The
# subdomain is the strongest geo filter: fr.linkedin.com is France-only, so
# "Paris" resolves to Paris, France instead of Paris, Texas.
_COUNTRY_SUBDOMAIN = {
    "india": "in", "united states": "www", "usa": "www", "us": "www",
    "united kingdom": "uk", "uk": "uk", "england": "uk", "scotland": "uk",
    "uae": "ae", "united arab emirates": "ae", "singapore": "sg",
    "france": "fr", "germany": "de", "spain": "es", "italy": "it",
    "netherlands": "nl", "canada": "ca", "australia": "au", "ireland": "ie",
    "switzerland": "ch", "belgium": "be", "sweden": "se", "brazil": "br",
}

# Map a city to its country so a city-only input still picks the right subdomain.
_CITY_COUNTRY = {
    "paris": "france", "lyon": "france", "toulouse": "france", "marseille": "france",
    "london": "uk", "manchester": "uk", "birmingham": "uk", "edinburgh": "uk",
    "dubai": "uae", "abu dhabi": "uae", "sharjah": "uae",
    "singapore": "singapore",
    "berlin": "germany", "munich": "germany", "hamburg": "germany", "frankfurt": "germany",
    "madrid": "spain", "barcelona": "spain", "milan": "italy", "rome": "italy",
    "amsterdam": "netherlands", "toronto": "canada", "vancouver": "canada", "montreal": "canada",
    "sydney": "australia", "melbourne": "australia", "dublin": "ireland", "zurich": "switzerland",
    "bangalore": "india", "bengaluru": "india", "mumbai": "india", "delhi": "india",
    "new delhi": "india", "gurgaon": "india", "gurugram": "india", "hyderabad": "india",
    "pune": "india", "chennai": "india", "kolkata": "india", "noida": "india",
    "new york": "usa", "san francisco": "usa", "los angeles": "usa", "chicago": "usa",
    "austin": "usa", "boston": "usa", "seattle": "usa",
}


def _resolve_geo(location: str) -> tuple[str, str, str | None]:
    """Resolve a free-text location to (subdomain, place_for_query, country_name).

    place_for_query is what to quote in the x-ray query (the city if given, else
    the country). country_name lets us add a second country-level query."""
    l = (location or "").strip()
    low = l.lower()
    # Direct country match.
    if low in _COUNTRY_SUBDOMAIN:
        country = low
        return _COUNTRY_SUBDOMAIN[low], l, country
    # "City, Country" — try the trailing part as a country.
    parts = [p.strip() for p in re.split(r"[,/]", l) if p.strip()]
    for p in reversed(parts):
        if p.lower() in _COUNTRY_SUBDOMAIN:
            return _COUNTRY_SUBDOMAIN[p.lower()], parts[0], p.lower()
    # City lookup.
    city = parts[0] if parts else l
    country = _CITY_COUNTRY.get(city.lower())
    if country:
        return _COUNTRY_SUBDOMAIN.get(country, "www"), city, country
    # Unknown — search globally with the place as a keyword.
    return "www", l, None


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


async def _expand_roles(role: str, want_variants: bool) -> list[str]:
    """Return [role] plus close title variants. For large target counts we ask
    the LLM (chat.completions; needs a generous token budget on the reasoning
    deployment) for ~12 synonyms/adjacent titles to widen the net. Falls back to
    just the role on any failure. No Apollo — Azure tokens only."""
    role = (role or "").strip() or "Marketing Manager"
    if not want_variants:
        return [role]
    try:
        from openai import AsyncAzureOpenAI
        from core.config import settings
        if not settings.AZURE_OPENAI_KEY:
            return [role]
        client = AsyncAzureOpenAI(
            api_key=settings.AZURE_OPENAI_KEY,
            azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
            api_version=settings.AZURE_OPENAI_API_VERSION,
        )
        prompt = (
            f'Return ONLY a JSON array of 12 LinkedIn job-title variants closely related to '
            f'"{role}" (synonyms plus adjacent seniority/function). No prose, no markdown.'
        )
        resp = await client.chat.completions.create(
            model=settings.AZURE_OPENAI_LLM_DEPLOYMENT,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=2000,
        )
        text = (resp.choices[0].message.content or "").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end == -1:
            return [role]
        import json
        arr = json.loads(text[start:end + 1])
        variants = [role] + [v.strip() for v in arr if isinstance(v, str) and v.strip()]
        seen, out = set(), []
        for v in variants:
            k = v.lower()
            if k not in seen:
                seen.add(k); out.append(v)
        return out[:13]
    except Exception as exc:
        logger.warning("[WebDiscovery] role expansion failed: %s", exc)
        return [role]


def _build_queries(role_variants: list[str], locations: list[str], keywords: str | None) -> list[str]:
    """Geo-targeted x-ray queries: each role variant x the country subdomain x
    its hub cities. The subdomain (e.g. fr.linkedin.com) is the geo filter, so
    results stay in-country. DDG lite has no page offset, so role x city variety
    is how we reach high volume."""
    locs = _expand_locations([l for l in (locations or []) if l and l.strip()])
    kw = (keywords or "").strip()
    roles = role_variants or ["Marketing Manager"]

    queries: list[str] = []
    # Subdomain-only queries first (highest yield, most resilient to 202s).
    for role in roles:
        sub, _place, _country = _resolve_geo(locs[0]) if locs and locs[0] else ("www", "", None)
        if sub != "www":
            queries.append(f'site:{sub}.linkedin.com/in "{role}"')
        else:
            queries.append(f'site:linkedin.com/in "{role}"')
    # Then role x city.
    for role in roles:
        for loc in locs:
            if not loc:
                continue
            sub, place, country = _resolve_geo(loc)
            base = f'site:{sub}.linkedin.com/in "{role}"'
            queries.append(f'{base} "{place}"')
            if kw:
                queries.append(f'{base} "{kw}" "{place}"')
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
    on_batch=None,
) -> list[dict]:
    """Find public LinkedIn profiles matching the ICP — no login, no Apollo.

    on_batch: optional async callback(new_leads: list[dict]) invoked after each
    query with the freshly-found leads, so long high-volume runs can persist
    progressively instead of losing everything if interrupted."""
    # For larger targets, widen the net with LLM-generated role variants so we
    # can actually reach the requested volume from a single role + location.
    role_variants = await _expand_roles(target_role, want_variants=limit > 40)
    queries = _build_queries(role_variants, locations or [], keywords)
    logger.info(
        "[WebDiscovery] role=%r (%d variants) locations=%r -> %d queries (target %d leads)",
        target_role, len(role_variants), locations, len(queries), limit,
    )

    leads: list[dict] = []
    seen: set[str] = set()

    for i, q in enumerate(queries):
        if len(leads) >= limit:
            break
        results = await _search_with_retry(q)
        batch: list[dict] = []
        for item in results:
            parsed = _parse_result(item)
            if not parsed:
                continue
            key = _profile_key(parsed["profile_url"])
            if not key or key in seen:
                continue
            seen.add(key)
            leads.append(parsed)
            batch.append(parsed)
            if len(leads) >= limit:
                break
        if batch and on_batch is not None:
            try:
                await on_batch(batch)
            except Exception as exc:
                logger.warning("[WebDiscovery] on_batch persist failed: %s", exc)
        # Be polite to DDG between queries.
        if i < len(queries) - 1:
            await asyncio.sleep(_QUERY_DELAY_S)

    logger.info("[WebDiscovery] role=%r found %d unique leads", target_role, len(leads))
    return leads[:limit]
