"""LeadsForge people SEARCH for Bob — contact discovery (names, titles,
LinkedIn URLs). Search is free; the paid enrichment endpoints (phones/emails)
are deliberately NOT wired here — that is a later product phase.

Why this exists: run-10 forensics showed >50% of Context.dev spend went to
"people discovery" web searches that returned contacts for 1 of 12 companies.
A structured people database answers that question properly and for free.
"""

import logging

import requests

from core.config import settings

logger = logging.getLogger(__name__)

_BASE = "https://api.leadsforge.ai/public/v1"
_TIMEOUT = 30


class LeadsForgeError(Exception):
    pass


def _search(key: str, company: str, domain: str, titles: list | None,
            locations: list | None, seniorities: list | None, n: int) -> list[dict]:
    body: dict = {"limit": n,  # required by the API (400 without it)
                  "maxContactsPerCompany": n,
                  "companyRequired": True}
    if domain:
        body["companyDomains"] = {"include": [domain]}
    elif company:
        body["companyNames"] = {"include": [company.strip()]}
    else:
        raise LeadsForgeError("need a company name or domain")
    if titles:
        body["leadJobTitles"] = {"include": [t for t in titles if t][:10], "exactMatch": False}
    if seniorities:
        body["leadSeniorities"] = {"include": seniorities[:6]}
    if locations:
        body["leadLocations"] = {"include": locations[:4]}

    try:
        r = requests.post(f"{_BASE}/search", json=body,
                          headers={"Authorization": key}, timeout=_TIMEOUT)
    except requests.RequestException as e:
        raise LeadsForgeError(f"request failed ({e.__class__.__name__})")
    if r.status_code == 401:
        raise LeadsForgeError("authentication failed (check LEADSFORGE_API_KEY)")
    if r.status_code == 402:
        raise LeadsForgeError("LeadsForge credits exhausted")
    if r.status_code == 429:
        raise LeadsForgeError("LeadsForge rate limited, try later in the run")
    if not r.ok:
        raise LeadsForgeError(f"http {r.status_code}: {r.text[:200]}")

    leads = (r.json() or {}).get("leads") or []
    out = []
    for l in leads[:n]:
        loc = l.get("location") or {}
        comp = l.get("company") or {}
        out.append({
            "name": " ".join(p for p in [l.get("firstName"), l.get("lastName")] if p).strip(),
            "title": l.get("jobTitle") or "",
            "company": comp.get("website") or comp.get("domain") or "",
            "city": ", ".join(p for p in [loc.get("city"), loc.get("state"), loc.get("country")] if p),
            "linkedin_url": l.get("linkedinUrl") or "",
        })
    return [p for p in out if p["name"]]


def find_people(company: str = "", domain: str = "", titles: list[str] | None = None,
                locations: list[str] | None = None, seniorities: list[str] | None = None,
                limit: int = 8) -> tuple[list[dict], str]:
    """Two-step people search at a company. Returns (people, mode):

    mode 'titled'       — the title filter matched people
    mode 'all_people'   — no titled match; returning EVERYONE found at the
                          company (+location) so the caller picks the most
                          hiring-adjacent person (small startups rarely have
                          an HR-titled employee)
    mode 'not_found'    — the company itself has no people in the database
    """
    key = settings.LEADSFORGE_API_KEY
    if not key:
        raise LeadsForgeError("LEADSFORGE_API_KEY is not configured")

    n = max(1, min(int(limit or 8), 25))
    domain = (domain or "").strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").split("/")[0]

    if titles:
        people = _search(key, company, domain, titles, locations, seniorities, n)
        if people:
            return people, "titled"
    people = _search(key, company, domain, None, locations, None, max(n, 15))
    if people:
        return people, "all_people"
    if locations:
        # Location can be over-strict (profiles often lack a parsed city).
        people = _search(key, company, domain, None, None, None, max(n, 15))
        if people:
            return people, "all_people"
    return [], "not_found"
