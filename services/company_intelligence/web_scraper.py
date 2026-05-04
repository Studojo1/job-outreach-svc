"""Light company-website scraper.

Fetches a small set of high-signal pages on the company's domain — homepage,
about, products, blog, careers — and extracts structured text the LLM
fact-extractor can use to write specific per-lead reasoning. Designed to
fail soft — on any error we mark scrape_failed=True so we don't retry,
and the LLM falls back to whatever Apollo data we have.

Constraints:
- All paths fetched in parallel with a single httpx.AsyncClient per domain.
- 5s per-URL timeout (don't block the discovery pipeline).
- Respects robots.txt via urllib.robotparser.
- Truncates everything to keep token cost on the fact-extractor + justifier
  call bounded.

Returned shape (each section may be None if nothing useful was found):
    {
      "meta_title": str | None,
      "meta_description": str | None,
      "hero_text": str | None,           # h1+h2 from the homepage
      "summary": str | None,             # legacy combined blob (homepage+about), kept for backwards-compat
      "product_summary": str | None,     # /products | /platform | /solutions
      "blog_summary": str | None,        # /blog
      "careers_summary": str | None,     # /careers — actual job-page copy
    }
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
MAX_SECTION_CHARS = 800       # per non-summary section
MAX_TITLE_CHARS = 200
MAX_DESC_CHARS = 500

# Tags that almost never contain useful content.
_STRIP_TAGS = ("script", "style", "noscript", "svg", "footer", "nav", "header")

# Candidate paths grouped by section. We try each path in a group in order
# and use the first one that returns >=100 chars of HTML.
_SECTION_PATHS = {
    "home": [""],                                          # the homepage
    "about": ["about", "about-us", "company"],
    "product": ["products", "product", "platform", "solutions", "features"],
    "blog": ["blog", "news", "resources/blog"],
    "careers": ["careers", "jobs", "join-us", "team/careers"],
}


async def scrape_company_site(domain: str) -> Optional[Dict[str, str]]:
    """Fetch the high-signal pages for a domain in parallel and extract sections."""
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
        # Fan out: one task per section. Each task tries its candidate paths
        # in order and stops at the first that returns content. All sections
        # run in parallel so total wall time is bounded by the slowest section.
        section_results = await asyncio.gather(*[
            _fetch_section(client, base, paths)
            for paths in _SECTION_PATHS.values()
        ])

    sections: Dict[str, str] = {
        name: html for name, html in zip(_SECTION_PATHS.keys(), section_results) if html
    }
    if not sections:
        return None

    parsed = {name: _parse_html(html) for name, html in sections.items()}
    home = parsed.get("home", {})
    about = parsed.get("about", {})

    # Legacy "summary" — kept so the existing prompt path doesn't break
    # while we wire the new structured sections through.
    summary_parts = []
    if home.get("meta_description"):
        summary_parts.append(home["meta_description"])
    if home.get("hero_text"):
        summary_parts.append(home["hero_text"])
    if about.get("body_text"):
        summary_parts.append(about["body_text"][:MAX_BODY_CHARS])
    summary = " | ".join(p for p in summary_parts if p).strip()

    out = {
        "meta_title": (home.get("meta_title") or about.get("meta_title") or "")[:MAX_TITLE_CHARS] or None,
        "meta_description": (home.get("meta_description") or about.get("meta_description") or "")[:MAX_DESC_CHARS] or None,
        "hero_text": (home.get("hero_text") or about.get("hero_text") or "")[:MAX_DESC_CHARS] or None,
        "summary": summary[:MAX_BODY_CHARS] or None,
        # New per-section payloads — these are what the fact extractor uses
        # to reason about WHAT the company actually builds.
        "product_summary": _section_blob(parsed.get("product"))[:MAX_SECTION_CHARS] or None,
        "blog_summary": _section_blob(parsed.get("blog"))[:MAX_SECTION_CHARS] or None,
        "careers_summary": _section_blob(parsed.get("careers"))[:MAX_SECTION_CHARS] or None,
    }
    logger.info(
        "[SCRAPE] OK %s — sections=%s summary_len=%d product_len=%d blog_len=%d careers_len=%d",
        domain,
        list(sections.keys()),
        len(out["summary"] or ""),
        len(out["product_summary"] or ""),
        len(out["blog_summary"] or ""),
        len(out["careers_summary"] or ""),
    )
    return out


def _section_blob(parsed: Optional[Dict[str, str]]) -> str:
    """Concatenate the most useful bits from a parsed page into one blob."""
    if not parsed:
        return ""
    parts = []
    if parsed.get("meta_description"):
        parts.append(parsed["meta_description"])
    if parsed.get("hero_text"):
        parts.append(parsed["hero_text"])
    if parsed.get("body_text"):
        parts.append(parsed["body_text"])
    return " | ".join(p for p in parts if p).strip()


async def _fetch_section(client: httpx.AsyncClient, base: str, paths: list[str]) -> Optional[str]:
    """Try each candidate path for a section in order; return the first that
    returns content. Each request still respects the per-URL timeout."""
    for path in paths:
        url = base if path == "" else urljoin(base + "/", path)
        html = await _fetch(client, url)
        if html:
            return html
    return None


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
