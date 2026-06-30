"""Extra cookie-free job sources for Mesa — public JSON APIs, no auth, no Apollo.

Aggregated boards that answer "which companies are actively hiring right now and
for what roles". Each scraper returns dicts:
    {external_id, title, company, location, posted_date, url}
The LinkedIn source lives in linkedin_jobs.py; this registry unifies them all
behind one signature: scrape(keywords, location, date_posted, workplace_types,
experience_levels, max_results).
"""

import logging
import urllib.parse

import httpx

from services.mesa.linkedin_jobs import scrape_jobs as _linkedin_scrape

logger = logging.getLogger(__name__)

_H = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
_T = 20.0


def _kw_match(text: str, keywords: str) -> bool:
    toks = [t for t in (keywords or "").lower().split() if len(t) > 2]
    if not toks:
        return True
    low = (text or "").lower()
    return any(t in low for t in toks)


def _get(url: str):
    return httpx.get(url, headers=_H, timeout=_T, verify=False, follow_redirects=True)


def _src_linkedin(keywords, location, date_posted, workplace_types, experience_levels, max_results):
    jobs = _linkedin_scrape(keywords, location, date_posted, workplace_types,
                            experience_levels, max_pages=max(1, min(6, max_results // 10)))
    return [{
        "external_id": j["linkedin_job_id"], "title": j["title"], "company": j["company"],
        "location": j["location"], "posted_date": j.get("posted_date"), "url": j["url"],
    } for j in jobs]


def _src_remotive(keywords, location, *_):
    url = "https://remotive.com/api/remote-jobs?limit=80"
    if keywords:
        url += "&search=" + urllib.parse.quote(keywords)
    out = []
    try:
        for j in _get(url).json().get("jobs", []):
            out.append({
                "external_id": str(j.get("id")), "title": j.get("title", ""),
                "company": j.get("company_name", ""),
                "location": j.get("candidate_required_location") or "Remote",
                "posted_date": (j.get("publication_date") or "")[:10], "url": j.get("url", ""),
            })
    except Exception as e:  # noqa: BLE001
        logger.warning("[MESA] remotive failed: %s", e)
    return out


def _src_remoteok(keywords, location, *_):
    out = []
    try:
        data = _get("https://remoteok.com/api").json()
        rows = data[1:] if (data and isinstance(data[0], dict) and "legal" in str(data[0]).lower()) else data
        for j in rows:
            if not isinstance(j, dict) or not j.get("id"):
                continue
            text = f"{j.get('position', '')} {' '.join(j.get('tags', []) or [])}"
            if not _kw_match(text, keywords):
                continue
            out.append({
                "external_id": str(j.get("id")), "title": j.get("position", ""),
                "company": j.get("company", ""), "location": j.get("location") or "Remote",
                "posted_date": (j.get("date") or "")[:10], "url": j.get("url", ""),
            })
    except Exception as e:  # noqa: BLE001
        logger.warning("[MESA] remoteok failed: %s", e)
    return out


def _src_arbeitnow(keywords, location, *_):
    out = []
    try:
        for j in _get("https://www.arbeitnow.com/api/job-board-api").json().get("data", []):
            text = f"{j.get('title', '')} {' '.join(j.get('tags', []) or [])}"
            if not _kw_match(text, keywords):
                continue
            if location and location.lower() not in (j.get("location") or "").lower() and not j.get("remote"):
                continue
            loc = (j.get("location") or "") + (" · Remote" if j.get("remote") else "")
            out.append({
                "external_id": j.get("slug") or str(j.get("url")), "title": j.get("title", ""),
                "company": j.get("company_name", ""), "location": loc.strip(" ·"),
                "posted_date": "", "url": j.get("url", ""),
            })
    except Exception as e:  # noqa: BLE001
        logger.warning("[MESA] arbeitnow failed: %s", e)
    return out


_MUSE_CATS = [
    "Marketing", "Sales", "Software Engineering", "Data Science", "Design and UX",
    "Product Management", "Project Management", "Finance", "HR & Recruiting",
    "Operations", "Customer Service", "Business & Strategy", "Account Management",
]


def _src_themuse(keywords, location, *_):
    kw = (keywords or "").lower()
    chosen = [c for c in _MUSE_CATS if any(w in kw for w in c.lower().replace("&", " ").split())]
    if not chosen:
        chosen = ["Marketing", "Software Engineering", "Sales", "Data Science"]
    seen = {}
    try:
        for cat in chosen[:3]:
            url = f"https://www.themuse.com/api/public/jobs?page=0&descending=true&category={urllib.parse.quote(cat)}"
            if location:
                url += "&location=" + urllib.parse.quote(location)
            for j in _get(url).json().get("results", []):
                if keywords and not _kw_match(j.get("name", ""), keywords):
                    continue
                eid = str(j.get("id"))
                if not eid or eid in seen:
                    continue
                locs = ", ".join(l.get("name", "") for l in (j.get("locations") or [])[:2])
                seen[eid] = {
                    "external_id": eid, "title": j.get("name", ""),
                    "company": (j.get("company") or {}).get("name", ""),
                    "location": locs or "Flexible",
                    "posted_date": (j.get("publication_date") or "")[:10],
                    "url": (j.get("refs") or {}).get("landing_page", ""),
                }
    except Exception as e:  # noqa: BLE001
        logger.warning("[MESA] themuse failed: %s", e)
    return list(seen.values())


# Registry — every value has the same signature.
SOURCE_SCRAPERS = {
    "linkedin": _src_linkedin,
    "themuse": _src_themuse,
    "remotive": _src_remotive,
    "remoteok": _src_remoteok,
    "arbeitnow": _src_arbeitnow,
}
ALL_SOURCES = list(SOURCE_SCRAPERS.keys())
# Sensible defaults for a new search: LinkedIn + the broadest aggregators.
DEFAULT_SOURCES = ["linkedin", "themuse", "remotive"]
