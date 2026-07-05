"""Getro VC-portfolio job boards — cookie-free public JSON API. No auth, no Apollo.

VC talent boards (jobs.accel.com, jobs.blume.vc, ...) are Getro-powered Next.js
apps whose jobs come from a public API:
    POST https://api.getro.com/api/v2/collections/{NUMERIC_ID}/search/jobs
The numeric id lives in the board page's __NEXT_DATA__ -> props.pageProps.network.id.

Why it matters for Mesa: these boards aggregate roles across a whole VC's
portfolio of *funded* startups — exactly the "which funded companies are hiring
for this role right now" question, and the seed for the signal engine
(services/mesa/signals.py). Returns the standard Mesa job shape:
    {external_id, title, company, location, posted_date, url}
"""

import logging
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

# Board numeric collection ids (from each board's __NEXT_DATA__.network.id).
# Extend by fetching a board URL and reading network.id. Peak XV / Lightspeed /
# Nexus / Bessemer are NOT on Getro (other platforms) — omitted deliberately.
GETRO_BOARDS = {"accel": 8672, "blume": 32333}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_HEADERS = {
    "User-Agent": _UA,
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://jobs.blume.vc",
    "Referer": "https://jobs.blume.vc/",
}
_TIMEOUT = 25.0

_INDIA = ("india", "bengaluru", "bangalore", "mumbai", "delhi", "gurgaon", "gurugram",
          "noida", "hyderabad", "pune", "chennai", "kolkata", "ncr")

# Mesa experience level -> Getro seniority values.
_SENIORITY = {
    "internship": ["internship"], "entry": ["entry_level"], "associate": ["mid_senior"],
    "mid-senior": ["mid_senior", "senior"], "director": ["director", "vp"],
    "executive": ["vp", "cxo"],
}


def _seniorities(experience_levels: list[str]) -> list[str]:
    out: list[str] = []
    for e in experience_levels or []:
        out += _SENIORITY.get(e, [])
    # dedupe, preserve order
    return list(dict.fromkeys(out))


def _india_focus(location: str) -> bool:
    low = (location or "").lower()
    return any(c in low for c in _INDIA)


def _iso(epoch) -> str | None:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return None


def _query_board(cid: int, query: str, filters: dict, page: int) -> list[dict]:
    body = {"query": query or "", "page": page, "filters": filters}
    r = httpx.post(f"https://api.getro.com/api/v2/collections/{cid}/search/jobs",
                   json=body, headers=_HEADERS, timeout=_TIMEOUT, verify=False)
    r.raise_for_status()
    return (r.json().get("results") or {}).get("jobs") or []


def scrape_jobs(keywords, location, date_posted, workplace_types, experience_levels, max_results):
    """One keyword term across all configured Getro boards. Server-filters by
    India location + seniority where possible; falls back to client-side location
    match. Returns standard Mesa job dicts."""
    india = _india_focus(location)
    filters: dict = {}
    if india:
        filters["searchable_locations"] = ["India"]
    sens = _seniorities(experience_levels)
    if sens:
        filters["seniorities"] = sens

    out: list[dict] = []
    seen: set = set()
    per_board = max(1, min(6, (max_results // len(GETRO_BOARDS)) // 20 + 1))
    for name, cid in GETRO_BOARDS.items():
        try:
            first = _query_board(cid, keywords, filters, 1)
        except Exception as e:  # noqa: BLE001 — one board must not sink the rest
            logger.warning("[MESA] getro/%s failed: %s", name, e)
            continue
        jobs = list(first)
        for p in range(2, per_board + 1):
            try:
                more = _query_board(cid, keywords, filters, p)
            except Exception:  # noqa: BLE001
                break
            if not more:
                break
            jobs += more
        for j in jobs:
            jid = j.get("id")
            if not jid or jid in seen:
                continue
            org = j.get("organization") or {}
            locs = j.get("searchable_locations") or j.get("locations") or []
            loc = locs[0] if locs else ""
            # client-side location guard when caller asked for a specific place
            if location and not india:
                blob = " ".join(str(x) for x in locs).lower()
                if location.lower() not in blob:
                    continue
            seen.add(jid)
            out.append({
                "external_id": f"getro-{jid}",
                "title": j.get("title", ""),
                "company": org.get("name", ""),
                "location": loc,
                "posted_date": _iso(j.get("created_at")),
                "url": j.get("url", ""),
            })
            if len(out) >= max_results:
                return out
    return out
