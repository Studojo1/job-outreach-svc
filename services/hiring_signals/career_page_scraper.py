"""Career page scraper — finds open job listings on a company's website.

Uses httpx for HTTP fetching and BeautifulSoup for HTML parsing.
JS-rendered pages (React/Next.js career sites) will return an empty
body — those are flagged as "js_rendered" and skipped gracefully.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Common career page path suffixes to try in order
_CAREER_PATHS = [
    "/careers",
    "/jobs",
    "/open-positions",
    "/join-us",
    "/work-with-us",
    "/about/careers",
    "/company/careers",
    "/careers/jobs",
    "/careers/open-roles",
    "/en/careers",
    "/en/jobs",
]

# CSS class / id fragments that commonly wrap job listings
_JOB_CONTAINER_PATTERNS = [
    "job", "position", "opening", "role", "vacancy", "career",
    "listing", "opportunity",
]

# Headers that mimic a real browser to avoid bot blocks
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

_TIMEOUT = 8.0  # seconds per request


async def scrape_career_page(domain: str, company_name: str = "") -> dict:
    """Try to find and parse a company's career page.

    Args:
        domain: Company domain, e.g. "razorpay.com" or "stripe.com".
                Can include http/https — will be normalised.
        company_name: Optional display name for logging.

    Returns dict with keys:
        status: "success" | "not_found" | "js_rendered" | "error"
        careers_url: URL that was scraped (if found)
        open_roles: list of {title, department, location}
        open_roles_count: int
        engineering_roles_count: int
        scrape_method: "httpx" | "js_rendered_skipped" | None
        scraped_at: ISO timestamp
        error: str (only on status="error")
    """
    domain = _normalise_domain(domain)
    label = company_name or domain
    logger.info("[CareerScraper] Scraping %s", label)

    async with httpx.AsyncClient(
        headers=_HEADERS,
        follow_redirects=True,
        timeout=_TIMEOUT,
        verify=False,  # some company sites have cert issues
    ) as client:
        careers_url, html = await _find_careers_page(client, domain)

    if not careers_url:
        return _result("not_found", careers_url=None, roles=[])

    if not html or len(html.strip()) < 500:
        # Likely a JS-rendered page that returned an empty shell
        return _result("js_rendered", careers_url=careers_url, roles=[])

    roles = _parse_roles(html, careers_url)

    return _result(
        "success",
        careers_url=careers_url,
        roles=roles,
        scrape_method="httpx",
    )


async def _find_careers_page(
    client: httpx.AsyncClient, domain: str
) -> tuple[Optional[str], Optional[str]]:
    """Try each candidate path and return (url, html) for the first that works."""
    for path in _CAREER_PATHS:
        url = f"https://{domain}{path}"
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                html = resp.text
                # Check it's not an error page / empty shell
                if len(html) > 1000 and not _is_404_page(html, path):
                    logger.info("[CareerScraper] Found careers page at %s", url)
                    return url, html
        except (httpx.RequestError, httpx.TimeoutException):
            continue

    # Fallback: try HTTP
    for path in _CAREER_PATHS[:3]:
        url = f"http://{domain}{path}"
        try:
            resp = await client.get(url)
            if resp.status_code == 200 and len(resp.text) > 1000:
                return url, resp.text
        except (httpx.RequestError, httpx.TimeoutException):
            continue

    return None, None


def _is_404_page(html: str, path: str) -> bool:
    """Heuristic: is this a 404/not-found page in disguise?"""
    lower = html.lower()
    markers = ["page not found", "404", "doesn't exist", "no page", "not found"]
    hits = sum(1 for m in markers if m in lower)
    return hits >= 2


def _parse_roles(html: str, base_url: str) -> list[dict]:
    """Extract job titles from the careers page HTML."""
    soup = BeautifulSoup(html, "lxml")

    # Remove nav, footer, header to reduce noise
    for tag in soup.find_all(["nav", "footer", "header", "script", "style"]):
        tag.decompose()

    roles = []
    seen_titles: set[str] = set()

    # Strategy 1: look for elements with job-related class/id names
    for pattern in _JOB_CONTAINER_PATTERNS:
        for el in soup.find_all(
            attrs={"class": re.compile(pattern, re.I)}
        ):
            title = _extract_title_from_element(el)
            if title and title not in seen_titles:
                seen_titles.add(title)
                roles.append(_build_role(title, el))

    # Strategy 2: look for <li> or <div> elements inside a container with job-ish text
    if not roles:
        for el in soup.find_all(["h2", "h3", "h4", "li", "a"]):
            text = el.get_text(strip=True)
            if _looks_like_job_title(text) and text not in seen_titles:
                seen_titles.add(text)
                roles.append(_build_role(text, el))

    # Deduplicate and cap
    return roles[:80]


def _extract_title_from_element(el) -> Optional[str]:
    # Try heading tags first
    for heading in el.find_all(["h2", "h3", "h4", "h5"]):
        text = heading.get_text(strip=True)
        if _looks_like_job_title(text):
            return text
    # Fall back to direct text
    text = el.get_text(strip=True)
    if _looks_like_job_title(text):
        return text[:120]
    return None


def _looks_like_job_title(text: str) -> bool:
    if not text or len(text) < 5 or len(text) > 100:
        return False
    # Skip obvious non-titles
    skip_words = {
        "apply", "learn more", "view", "read", "click", "here", "back",
        "next", "previous", "all jobs", "all positions", "see all",
        "no open", "sorry", "currently", "browse", "filter", "sort",
        "department", "location", "remote", "full-time", "part-time",
    }
    lower = text.lower()
    if lower in skip_words or any(lower.startswith(s) for s in skip_words):
        return False
    # Must contain at least one letter word of 3+ chars
    words = re.findall(r"[a-zA-Z]{3,}", text)
    if len(words) < 2:
        return False
    # Job title heuristic: typically 2-7 words
    word_count = len(text.split())
    return 2 <= word_count <= 10


def _build_role(title: str, el) -> dict:
    role: dict = {"title": title}
    # Try to infer department and location from surrounding text
    parent_text = ""
    if el.parent:
        parent_text = el.parent.get_text(" ", strip=True).lower()
    role["department"] = _infer_department(title, parent_text)
    role["location"] = _infer_location(parent_text)
    return role


def _infer_department(title: str, context: str) -> Optional[str]:
    text = (title + " " + context).lower()
    mapping = {
        "Engineering": ["engineer", "developer", "backend", "frontend", "devops", "sre", "data", "ml", "ai", "platform", "infrastructure", "software"],
        "Product": ["product manager", "product owner", "pm ", "product lead"],
        "Design": ["designer", "ux", "ui", "creative"],
        "Sales": ["sales", "account executive", "ae ", "sdr ", "bdr "],
        "Marketing": ["marketing", "growth", "content", "seo", "brand"],
        "Operations": ["operations", "ops", "supply chain", "logistics"],
        "Finance": ["finance", "accounting", "controller", "analyst"],
        "HR/People": ["recruiter", "talent", "hr ", "people ops", "people partner"],
    }
    for dept, keywords in mapping.items():
        if any(kw in text for kw in keywords):
            return dept
    return None


def _infer_location(context: str) -> Optional[str]:
    # Simple city name detection
    cities = [
        "bangalore", "bengaluru", "mumbai", "delhi", "hyderabad", "pune",
        "chennai", "kolkata", "gurgaon", "noida", "san francisco", "new york",
        "london", "singapore", "remote", "hybrid", "anywhere", "india", "us",
    ]
    for city in cities:
        if city in context:
            return city.title()
    return None


def _normalise_domain(domain: str) -> str:
    domain = domain.strip().lower()
    # Strip protocol if present
    for prefix in ["https://", "http://", "www."]:
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    # Strip path
    domain = domain.split("/")[0]
    return domain


def _result(
    status: str,
    careers_url: Optional[str],
    roles: list,
    scrape_method: str = "httpx",
) -> dict:
    eng_count = sum(
        1 for r in roles if r.get("department") == "Engineering"
    )
    return {
        "status": status,
        "careers_url": careers_url,
        "open_roles": roles,
        "open_roles_count": len(roles),
        "engineering_roles_count": eng_count,
        "scrape_method": scrape_method if status not in ("not_found", "error") else None,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }
