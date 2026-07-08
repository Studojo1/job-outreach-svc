"""Unstop internships — free structured source for Bob (zero Context.dev credits).

Unstop exposes a public, no-auth JSON API over its full internship pool
(~10k open). Unlike a web-search that returns listing pages, this hands back
INDIVIDUAL postings with structured stipend, deadline, eligibility, location
and skills. It is the India intern market's biggest single source and the main
cross-platform gap vs the Mesa scraper.

Key API facts (validated 2026-07-09):
  GET /api/public/opportunity/search-result
      opportunity=internships   (required)
      oppstatus=open            (REQUIRED for liveness — without it the feed
                                 returns postings with 2021 deadlines still
                                 marked regn_open=1; oppstatus=open filters to
                                 genuinely-open, future-deadline roles)
      searchTerm=<keywords>     (fuzzy title/skill match; filter noise client-side)
      sort=latest, per_page=N, page=N
  Location is NOT reliably server-filterable, so we filter client-side; many
  roles are region=online/india (remote-eligible, kept for any city mandate).
"""

import logging
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

_URL = "https://unstop.com/api/public/opportunity/search-result"
_TIMEOUT = 25
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


class UnstopError(Exception):
    pass


def _future(end_date: str) -> bool:
    if not end_date:
        return True  # no deadline stated -> do not exclude
    try:
        return datetime.fromisoformat(end_date) > datetime.now().astimezone()
    except (ValueError, TypeError):
        return True


def _loc_names(item: dict) -> list[str]:
    out = []
    for l in (item.get("locations") or []):
        if isinstance(l, dict) and l.get("name"):
            out.append(l["name"])
        elif isinstance(l, str) and l:
            out.append(l)
    jd = item.get("jobDetail") or {}
    for l in (jd.get("locations") or []):
        if isinstance(l, dict) and l.get("name"):
            out.append(l["name"])
        elif isinstance(l, str) and l:
            out.append(l)
    return out


def _stipend(jd: dict) -> str:
    if not jd or jd.get("not_disclosed") or jd.get("show_salary") is False:
        return ""
    lo, hi = jd.get("min_salary"), jd.get("max_salary")
    if not lo and not hi:
        return ""
    unit = "/month" if (jd.get("pay_in") or "").lower() in ("month", "monthly") else ""
    if lo and hi and lo != hi:
        return f"Rs {lo}-{hi}{unit}"
    return f"Rs {lo or hi}{unit}"


def search_internships(keywords: str, location: str = "", limit: int = 20) -> list[dict]:
    """Open internships matching keywords. Returns individual postings with
    structured fields. Raises UnstopError on transport failure."""
    params = {
        "opportunity": "internships",
        "oppstatus": "open",
        "sort": "latest",
        "searchTerm": keywords or "",
        "per_page": max(10, min(int(limit or 20) * 2, 60)),  # over-fetch; we filter
        "page": 1,
    }
    try:
        r = requests.get(_URL, params=params, headers=_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException as e:
        raise UnstopError(f"request failed ({e.__class__.__name__})")
    if not r.ok:
        raise UnstopError(f"http {r.status_code}")
    data = (r.json() or {}).get("data") or {}
    items = data.get("data") or [] if isinstance(data, dict) else []

    city = (location or "").strip().lower()
    city_alias = {"bengaluru": "bangalor", "bangalore": "bangalor"}.get(city, city)
    out = []
    for it in items:
        if not _future(it.get("end_date")):
            continue
        locs = _loc_names(it)
        region = (it.get("region") or "").lower()
        # region=online/WFH means the role is remote even when jobDetail carries
        # the company's HQ city — a remote role is accessible from any city, so
        # keep it and LABEL it "Remote" (showing the HQ city would mislead).
        is_remote = region in ("online", "work from home") or (region in ("india", "") and not locs)
        if city_alias:
            loc_hit = any(city_alias in l.lower() for l in locs)
            if not (loc_hit or is_remote):
                continue
        org = it.get("organisation") or {}
        elig = [f["name"] for f in (it.get("filters") or [])
                if isinstance(f, dict) and f.get("type") == "eligible" and f.get("name")]
        skills = [s.get("name") for s in (it.get("required_skills") or [])
                  if isinstance(s, dict) and s.get("name")]
        out.append({
            "title": it.get("title") or "",
            "company": (org.get("name") if isinstance(org, dict) else "") or "",
            "url": f"https://unstop.com/{it.get('public_url')}" if it.get("public_url") else "",
            "location": "Remote" if region in ("online", "work from home") else (", ".join(dict.fromkeys(locs)) or ("Remote/India" if is_remote else "")),
            "stipend": _stipend(it.get("jobDetail") or {}),
            "deadline": (it.get("end_date") or "")[:10],
            "eligibility": ", ".join(elig),
            "skills": ", ".join(skills[:6]),
        })
        if len(out) >= limit:
            break
    return out
