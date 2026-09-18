"""Google News RSS search — free, no API key, no rate limits at low volume."""

import logging
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

_TIMEOUT = 8.0
_RSS_BASE = "https://news.google.com/rss/search"


async def get_company_news(company_name: str, max_results: int = 8) -> list[dict]:
    """Fetch recent news about a company via Google News RSS.

    Returns list of {headline, url, published_at, source}.
    Returns empty list on any failure — never raises.
    """
    query = f'"{company_name}" funding OR hiring OR expansion OR "series A" OR "series B" OR headcount OR "raised"'
    url = f"{_RSS_BASE}?q={quote_plus(query)}&hl=en-IN&gl=IN&ceid=IN:en"

    try:
        async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=_TIMEOUT) as client:
            resp = await client.get(url)

        if resp.status_code != 200:
            logger.warning("[NewsRSS] Non-200 for company=%r: %d", company_name, resp.status_code)
            return []

        root = ET.fromstring(resp.text)
        channel = root.find("channel")
        if channel is None:
            return []

        results = []
        for item in channel.findall("item")[:max_results]:
            title = _text(item, "title")
            link = _text(item, "link")
            pub_date = _text(item, "pubDate")
            source_el = item.find("source")
            source = source_el.text if source_el is not None else None

            if title and link:
                results.append({
                    "headline": title,
                    "url": link,
                    "published_at": pub_date,
                    "source": source,
                })

        logger.info("[NewsRSS] company=%r returned %d items", company_name, len(results))
        return results

    except Exception as exc:
        logger.warning("[NewsRSS] Failed for company=%r: %s", company_name, exc)
        return []


def _text(el: ET.Element, tag: str) -> str | None:
    child = el.find(tag)
    return child.text.strip() if child is not None and child.text else None
