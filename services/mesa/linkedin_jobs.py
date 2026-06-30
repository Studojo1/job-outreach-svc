"""Cookie-free LinkedIn job scraping via the public guest jobs endpoint.

Uses LinkedIn's unauthenticated /jobs-guest/jobs/api/seeMoreJobPostings/search
endpoint — no login, no cookies, no Apollo — routed through the residential
proxy (LINKEDIN_PROXY_URL) so the cluster's egress IP is never rate-limited.
Proven on live data: keywords, location, date-posted, workplace type, seniority,
pagination and dedupe all work without a session.
"""

import logging
import time
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup

from core.config import settings

logger = logging.getLogger(__name__)

_GUEST_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_TIMEOUT = 25.0
_PAGE_SIZE = 10  # LinkedIn returns ~10 cards per guest page

# UI value -> LinkedIn filter code. We accept either the friendly key or a raw code.
DATE_POSTED = {"24h": "r86400", "week": "r604800", "month": "r2592000", "any": ""}
WORKPLACE = {"on-site": "1", "onsite": "1", "remote": "2", "hybrid": "3"}
EXPERIENCE = {
    "internship": "1", "entry": "2", "associate": "3",
    "mid-senior": "4", "director": "5", "executive": "6",
}


def _proxy() -> str | None:
    return (getattr(settings, "LINKEDIN_PROXY_URL", "") or "").strip() or None


def _parse(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for card in soup.select("li"):
        title_el = card.select_one(".base-search-card__title")
        if not title_el:
            continue
        base = card.select_one("[data-entity-urn]")
        urn = (base.get("data-entity-urn") if base else "") or ""
        job_id = urn.split(":")[-1] if urn else ""
        if not job_id:
            continue
        comp = card.select_one(".base-search-card__subtitle")
        loc = card.select_one(".job-search-card__location")
        tm = card.select_one("time")
        link = card.select_one("a.base-card__full-link") or card.select_one("a")
        out.append({
            "linkedin_job_id": job_id,
            "title": title_el.get_text(strip=True),
            "company": comp.get_text(strip=True) if comp else "",
            "location": loc.get_text(strip=True) if loc else "",
            "posted_date": (tm.get("datetime") if tm else "") or None,
            "url": ((link.get("href") if link else "") or "").split("?")[0],
        })
    return out


def scrape_jobs(
    keywords: str,
    location: str = "",
    date_posted: str = "24h",
    workplace_types: list[str] | None = None,
    experience_levels: list[str] | None = None,
    max_pages: int = 4,
) -> list[dict]:
    """Return de-duplicated job dicts for one search. Cookie-free, proxied.

    Stops early when a page is empty or yields nothing new (end of results).
    """
    params: dict = {"keywords": keywords or ""}
    if location:
        params["location"] = location
    tpr = DATE_POSTED.get(date_posted, date_posted or "")
    if tpr:
        params["f_TPR"] = tpr
    wt = [WORKPLACE.get(w, w) for w in (workplace_types or []) if w]
    if wt:
        params["f_WT"] = ",".join(wt)
    el = [EXPERIENCE.get(e, e) for e in (experience_levels or []) if e]
    if el:
        params["f_E"] = ",".join(el)

    seen: dict[str, dict] = {}
    proxy = _proxy()
    try:
        with httpx.Client(headers=_HEADERS, timeout=_TIMEOUT, verify=False,
                          proxy=proxy, follow_redirects=True) as client:
            for page in range(max_pages):
                q = dict(params, start=page * _PAGE_SIZE)
                try:
                    r = client.get(_GUEST_URL + "?" + urlencode(q))
                except Exception as e:  # noqa: BLE001
                    logger.warning("[MESA] scrape error page %d: %s", page, e)
                    break
                if r.status_code != 200:
                    logger.info("[MESA] stop at page %d (HTTP %d)", page, r.status_code)
                    break
                jobs = _parse(r.text)
                if not jobs:
                    break
                added = 0
                for j in jobs:
                    if j["linkedin_job_id"] not in seen:
                        seen[j["linkedin_job_id"]] = j
                        added += 1
                if added == 0:
                    break  # no new ids -> reached the tail
                if page < max_pages - 1:
                    time.sleep(1.5)  # polite pacing through the proxy
    except Exception as e:  # noqa: BLE001 — never let a scrape crash the caller
        logger.error("[MESA] scrape_jobs failed kw=%r: %s", keywords, e)

    logger.info("[MESA] scraped %d unique jobs (kw=%r, loc=%r)", len(seen), keywords, location)
    return list(seen.values())
