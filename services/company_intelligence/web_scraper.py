"""Light company-website scraper.

Fetches the company homepage + /about and extracts a small structured payload
the LLM can use to write specific per-lead reasoning. Designed to fail soft —
on any error we mark scrape_failed=True so we don't retry, and the LLM falls
back to Apollo data only.

Constraints:
- Single httpx.AsyncClient with global concurrency limit.
- 5s per-URL timeout (don't block the discovery pipeline).
- Respects robots.txt via urllib.robotparser.
- Truncates everything to keep token cost on the LLM call bounded.
"""

import asyncio
import logging
from typing import Dict, Optional
from urllib import robotparser
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENT = "StudojoBot/1.0 (+https://studojo.com; contact: hi@studojo.com)"
PER_URL_TIMEOUT = 5.0
MAX_BODY_CHARS = 2000
MAX_TITLE_CHARS = 200
MAX_DESC_CHARS = 500

# Tags that almost never contain useful content.
_STRIP_TAGS = ("script", "style", "noscript", "svg", "footer", "nav", "header")


async def scrape_company_site(domain: str) -> Optional[Dict[str, str]]:
    """Fetch homepage + /about for a domain and extract structured fields.

    Returns dict with keys: meta_title, meta_description, hero_text, summary.
    Returns None on robots.txt disallow, network error, or empty extract.
    """
    if not domain:
        return None

    base = f"https://{domain.strip().lstrip('https://').lstrip('http://').rstrip('/')}"

    if not await _robots_allows(base):
        logger.info("[SCRAPE] robots.txt disallows %s", base)
        return None

    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"}

    async with httpx.AsyncClient(
        timeout=PER_URL_TIMEOUT,
        follow_redirects=True,
        headers=headers,
    ) as client:
        home = await _fetch(client, base)
        about = await _fetch(client, urljoin(base + "/", "about"))

    if not home and not about:
        return None

    parsed_home = _parse_html(home) if home else {}
    parsed_about = _parse_html(about) if about else {}

    summary_parts = []
    if parsed_home.get("meta_description"):
        summary_parts.append(parsed_home["meta_description"])
    if parsed_home.get("hero_text"):
        summary_parts.append(parsed_home["hero_text"])
    if parsed_about.get("body_text"):
        summary_parts.append(parsed_about["body_text"][:MAX_BODY_CHARS])

    summary = " | ".join(s for s in summary_parts if s).strip()
    if not summary:
        return None

    out = {
        "meta_title": (parsed_home.get("meta_title") or parsed_about.get("meta_title") or "")[:MAX_TITLE_CHARS] or None,
        "meta_description": (parsed_home.get("meta_description") or parsed_about.get("meta_description") or "")[:MAX_DESC_CHARS] or None,
        "hero_text": (parsed_home.get("hero_text") or parsed_about.get("hero_text") or "")[:MAX_DESC_CHARS] or None,
        "summary": summary[:MAX_BODY_CHARS] or None,
    }
    logger.info("[SCRAPE] OK %s — title=%r desc_len=%d summary_len=%d",
                domain, out["meta_title"], len(out.get("meta_description") or ""), len(out["summary"] or ""))
    return out


async def _robots_allows(base_url: str) -> bool:
    """Best-effort robots.txt check. On fetch failure, allow (fail-open) —
    most companies don't have a relevant Disallow for static homepage assets."""
    rp = robotparser.RobotFileParser()
    try:
        async with httpx.AsyncClient(timeout=3.0, headers={"User-Agent": USER_AGENT}) as client:
            r = await client.get(f"{base_url}/robots.txt")
            if r.status_code == 200 and r.text:
                rp.parse(r.text.splitlines())
                return rp.can_fetch(USER_AGENT, base_url + "/")
    except Exception:
        pass
    return True


async def _fetch(client: httpx.AsyncClient, url: str) -> Optional[str]:
    try:
        r = await client.get(url)
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        logger.debug("[SCRAPE] fetch error %s: %s", url, e)
        return None
    if r.status_code != 200:
        return None
    ctype = r.headers.get("content-type", "")
    if "html" not in ctype.lower():
        return None
    if not r.text or len(r.text) < 100:
        return None
    return r.text[:200_000]  # cap before parsing


def _parse_html(html: str) -> Dict[str, str]:
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return {}

    for tag in soup(_STRIP_TAGS):
        tag.decompose()

    meta_title = (soup.title.string.strip() if soup.title and soup.title.string else "")
    meta_desc = ""
    desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if desc_tag and desc_tag.get("content"):
        meta_desc = desc_tag["content"].strip()

    hero_parts = []
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        hero_parts.append(h1.get_text(strip=True))
    h2 = soup.find("h2")
    if h2 and h2.get_text(strip=True):
        hero_parts.append(h2.get_text(strip=True))

    body = soup.body or soup
    body_text = " ".join(t.strip() for t in body.stripped_strings)

    return {
        "meta_title": meta_title,
        "meta_description": meta_desc,
        "hero_text": " — ".join(hero_parts) if hero_parts else "",
        "body_text": body_text,
    }


async def scrape_many(domains: list[str], concurrency: int = 10) -> Dict[str, Optional[Dict[str, str]]]:
    """Fan out scrapes across `domains` with a global concurrency limit."""
    sem = asyncio.Semaphore(concurrency)
    out: Dict[str, Optional[Dict[str, str]]] = {}

    async def _worker(d: str):
        async with sem:
            try:
                out[d] = await scrape_company_site(d)
            except Exception as e:
                logger.warning("[SCRAPE] uncaught error for %s: %s", d, e)
                out[d] = None

    await asyncio.gather(*(_worker(d) for d in domains))
    return out
