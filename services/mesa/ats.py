"""ATS career-board scrapers for Mesa — the cleanest hiring signal there is:
each company's own openings, straight from their applicant-tracking system's
public JSON API. No auth, no captcha, fresh, structured.

Greenhouse / Lever / Ashby / Workable each expose a per-company board. We keep a
curated slug registry (extend freely — grow it from companies that keep surfacing
in the LinkedIn/board scrapes). Same source contract as sources.py:
    scrape(keywords, location, date_posted, workplace_types, experience_levels, max_results)
    -> [{external_id, title, company, location, posted_date, url}]
"""
import logging
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

_H = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
      "Accept": "application/json"}
_T = 20.0
# ATS boards list *currently open* reqs — an open role is the hiring signal even if
# it was published a few weeks ago, so the window is deliberately lenient (an ATS
# posting rarely lingers past ~6 weeks). Downstream scorers can still filter tighter
# on the exposed posted_date for a strict "last 7 days" candidate view.
_WINDOW = {"24h": 3, "week": 45, "month": 90, "any": 3650}

# Curated live slugs (validated). Mix of global tech + India-origin companies.
# Extend these lists to widen coverage — dead slugs simply return nothing.
GREENHOUSE = [  # validated live; India-origin: postman, druva, groww, slice
    "stripe", "postman", "gitlab", "coinbase", "databricks", "dropbox", "cloudflare",
    "discord", "robinhood", "instacart", "gusto", "samsara", "affirm", "asana",
    "elastic", "mongodb", "datadog", "pinterest", "reddit", "airtable", "scaleai",
    "anthropic", "twilio", "brex", "druva", "groww", "slice", "figma",
]
ASHBY = [  # validated live
    "ramp", "posthog", "Notion", "linear", "replit", "vanta", "runway",
    "moderntreasury", "airbyte", "astronomer", "temporal", "supabase", "neon",
    "resend", "substack", "whoop", "harvey", "decagon", "sierra", "watershed",
    "openai", "cursor",
]
LEVER = [  # validated live; India-origin: meesho
    "spotify", "highspot", "mistral", "gopuff", "palantir", "meesho",
]
# Workable's widget endpoint answers but the accounts we tried had 0 open reqs.
# Add valid Workable account slugs here to activate (kept in BETA meanwhile).
WORKABLE = ["make", "remote", "deel"]


def _within(dt: datetime | None, date_posted: str) -> bool:
    if dt is None:
        return True  # open req, no reliable date -> keep
    try:
        days = (datetime.now(timezone.utc) - dt).days
    except Exception:
        return True
    return 0 <= days <= _WINDOW.get(date_posted, 31)


def _match(text: str, toks: list[str]) -> bool:
    if not toks:
        return True
    low = (text or "").lower()
    return any(t in low for t in toks)


def _toks(keywords: str) -> list[str]:
    return [t for t in (keywords or "").lower().split() if len(t) > 2]


def _get(url: str):
    return httpx.get(url, headers=_H, timeout=_T, verify=False, follow_redirects=True)


# ── Greenhouse ────────────────────────────────────────────────────────────────
def scrape_greenhouse(keywords, location, date_posted="week", workplace_types=None,
                      experience_levels=None, max_results=120):
    toks = _toks(keywords)
    out: list[dict] = []
    for slug in GREENHOUSE:
        if len(out) >= max_results:
            break
        try:
            r = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false")
            if r.status_code != 200:
                continue
            jobs = r.json().get("jobs", [])
        except Exception as e:  # noqa: BLE001
            logger.debug("[MESA] greenhouse %s: %s", slug, e)
            continue
        for j in jobs:
            title = j.get("title") or ""
            loc = (j.get("location") or {}).get("name") or ""
            depts = " ".join(d.get("name", "") for d in (j.get("departments") or []))
            if not _match(f"{title} {depts}", toks):
                continue
            try:
                dt = datetime.fromisoformat((j.get("updated_at") or "").replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                dt = None
            if not _within(dt, date_posted):
                continue
            out.append({"external_id": f"gh_{slug}_{j.get('id')}", "title": title.strip(),
                        "company": slug.replace("-", " ").title(), "location": loc or "-",
                        "posted_date": dt.date().isoformat() if dt else "",
                        "url": j.get("absolute_url") or ""})
    return out[:max_results]


# ── Ashby ─────────────────────────────────────────────────────────────────────
def scrape_ashby(keywords, location, date_posted="week", workplace_types=None,
                 experience_levels=None, max_results=120):
    toks = _toks(keywords)
    out: list[dict] = []
    for slug in ASHBY:
        if len(out) >= max_results:
            break
        try:
            r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false")
            if r.status_code != 200:
                continue
            jobs = r.json().get("jobs", [])
        except Exception as e:  # noqa: BLE001
            logger.debug("[MESA] ashby %s: %s", slug, e)
            continue
        for j in jobs:
            title = j.get("title") or ""
            dept = f"{j.get('department', '')} {j.get('team', '')}"
            if not _match(f"{title} {dept}", toks):
                continue
            try:
                dt = datetime.fromisoformat((j.get("publishedAt") or "").replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                dt = None
            if not _within(dt, date_posted):
                continue
            loc = j.get("location") or ("Remote" if j.get("isRemote") else "-")
            out.append({"external_id": f"ashby_{slug}_{j.get('id')}", "title": title.strip(),
                        "company": slug.title(), "location": loc,
                        "posted_date": dt.date().isoformat() if dt else "",
                        "url": j.get("jobUrl") or j.get("applyUrl") or ""})
    return out[:max_results]


# ── Lever ─────────────────────────────────────────────────────────────────────
def scrape_lever(keywords, location, date_posted="week", workplace_types=None,
                 experience_levels=None, max_results=120):
    toks = _toks(keywords)
    out: list[dict] = []
    for slug in LEVER:
        if len(out) >= max_results:
            break
        try:
            r = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
            if r.status_code != 200:
                continue
            jobs = r.json()
        except Exception as e:  # noqa: BLE001
            logger.debug("[MESA] lever %s: %s", slug, e)
            continue
        for j in jobs:
            title = j.get("text") or ""
            cats = j.get("categories") or {}
            if not _match(f"{title} {cats.get('team', '')}", toks):
                continue
            try:
                dt = datetime.fromtimestamp(int(j.get("createdAt")) / 1000, tz=timezone.utc)
            except Exception:
                dt = None
            if not _within(dt, date_posted):
                continue
            out.append({"external_id": f"lever_{slug}_{j.get('id')}", "title": title.strip(),
                        "company": slug.replace("-", " ").title(),
                        "location": cats.get("location") or ("Remote" if j.get("workplaceType") == "remote" else "-"),
                        "posted_date": dt.date().isoformat() if dt else "",
                        "url": j.get("hostedUrl") or j.get("applyUrl") or ""})
    return out[:max_results]


# ── Workable ──────────────────────────────────────────────────────────────────
def scrape_workable(keywords, location, date_posted="week", workplace_types=None,
                    experience_levels=None, max_results=100):
    toks = _toks(keywords)
    out: list[dict] = []
    for slug in WORKABLE:
        if len(out) >= max_results:
            break
        try:
            r = _get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
            if r.status_code != 200:
                continue
            data = r.json()
            jobs = data.get("jobs", [])
        except Exception as e:  # noqa: BLE001
            logger.debug("[MESA] workable %s: %s", slug, e)
            continue
        for j in jobs:
            title = j.get("title") or ""
            if not _match(f"{title} {j.get('department', '')}", toks):
                continue
            city = j.get("city") or ""
            country = j.get("country") or ""
            loc = ", ".join(x for x in (city, country) if x) or ("Remote" if j.get("remote") else "-")
            sc = j.get("shortcode") or ""
            url = j.get("url") or f"https://apply.workable.com/{slug}/j/{sc}"
            out.append({"external_id": f"wk_{slug}_{sc}", "title": title.strip(),
                        "company": data.get("name") or slug.title(), "location": loc,
                        "posted_date": (j.get("published_on") or "")[:10], "url": url})
    return out[:max_results]
