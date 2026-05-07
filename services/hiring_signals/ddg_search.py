"""DuckDuckGo web search — no API key, no rate limits at low volume."""

import logging
from typing import Optional
from urllib.parse import urlparse, unquote

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

_TIMEOUT = 8.0
_DDG_URL = "https://html.duckduckgo.com/html/"

# Domains to skip when inferring a company's official domain
_SKIP_DOMAINS = {
    "linkedin.com", "wikipedia.org", "glassdoor.com", "ambitionbox.com",
    "crunchbase.com", "zoominfo.com", "bloomberg.com", "forbes.com",
    "twitter.com", "x.com", "facebook.com", "instagram.com", "youtube.com",
    "tracxn.com", "owler.com", "dnb.com", "craft.co",
    "duckduckgo.com", "google.com", "bing.com", "yahoo.com",
    "indiamart.com", "justdial.com", "quora.com", "reddit.com",
    "moneycontrol.com", "economictimes.com", "livemint.com", "businessinsider.com",
}


async def ddg_search(query: str, max_results: int = 5) -> list[dict]:
    """Search DuckDuckGo and return structured results.

    Returns list of {title, url, snippet}.
    Returns empty list on any failure — never raises.
    """
    try:
        async with httpx.AsyncClient(
            headers=_HEADERS,
            follow_redirects=True,
            timeout=_TIMEOUT,
            verify=False,
        ) as client:
            resp = await client.post(_DDG_URL, data={"q": query})

        if resp.status_code != 200:
            logger.warning("[DDG] Non-200 for query=%r: %d", query, resp.status_code)
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        results = []

        for el in soup.select(".result")[:max_results * 2]:
            title_el = el.select_one(".result__title a")
            snippet_el = el.select_one(".result__snippet")
            if not title_el:
                continue

            raw_url = title_el.get("href", "")
            url = _clean_ddg_url(raw_url)
            title = title_el.get_text(strip=True)
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""

            if url and title:
                results.append({"title": title, "url": url, "snippet": snippet})

            if len(results) >= max_results:
                break

        logger.info("[DDG] query=%r returned %d results", query[:60], len(results))
        return results

    except Exception as exc:
        logger.warning("[DDG] Search failed for query=%r: %s", query[:60], exc)
        return []


def _clean_ddg_url(raw: str) -> Optional[str]:
    """DuckDuckGo wraps URLs in a redirect — unwrap to get the real URL.

    DDG HTML results use protocol-relative URLs like:
      //duckduckgo.com/l/?uddg=https%3A%2F%2Fstripe.com%2F&rut=...
    urlparse can't parse protocol-relative URLs without a scheme, so we
    prepend https: before parsing.
    """
    if not raw:
        return None
    # Normalise protocol-relative to absolute so urlparse works correctly
    if raw.startswith("//"):
        raw = "https:" + raw
    if "uddg=" in raw:
        try:
            from urllib.parse import parse_qs as _parse_qs
            parsed = urlparse(raw)
            uddg = _parse_qs(parsed.query).get("uddg", [None])[0]
            if uddg:
                return unquote(uddg)
        except Exception:
            pass
    if raw.startswith("http"):
        return raw
    return None


def extract_linkedin_profile(results: list[dict]) -> Optional[str]:
    """Return first linkedin.com/in/ URL from search results."""
    for r in results:
        url = r.get("url", "")
        if url and "linkedin.com/in/" in url:
            # Clean up any tracking params
            return url.split("?")[0]
    return None


def extract_linkedin_company(results: list[dict]) -> Optional[str]:
    """Return first linkedin.com/company/ URL from search results."""
    for r in results:
        url = r.get("url", "")
        if url and "linkedin.com/company/" in url:
            return url.split("?")[0]
    return None


def extract_company_domain(results: list[dict]) -> Optional[str]:
    """Return the most likely official company domain from search results.

    Skips known aggregator/social sites (LinkedIn, Wikipedia, Glassdoor, etc.)
    and returns the bare domain of the first remaining result.
    """
    for r in results:
        url = r.get("url", "")
        if not url:
            continue
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower().lstrip("www.")
            if domain and not any(skip in domain for skip in _SKIP_DOMAINS):
                return domain
        except Exception:
            continue
    return None
