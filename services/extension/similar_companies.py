"""When we cannot reach anyone at a company, suggest ones we can.

Why this exists
---------------
"We haven't found anyone at Pipraiser we can email yet" is a dead end. The
student did the work of finding a role they want and gets nothing back. But
the thing they actually want is not *that company* — it is a job like that
one, at a company like that one, where a real person will read their email.

So instead of stopping, we do for the extension what the outreach tool's
leads page already does for a campaign: take the industry, the size band and
the role, and find companies matching those criteria that we CAN reach.

How
---
Two Apollo calls, both mirroring the working lead-discovery flow:

1. ``/mixed_companies/search`` on the original company name, to read its
   industry, keyword tags and headcount band. This is the profile we match on.
2. ``/mixed_people/api_search`` filtered by that profile plus
   ``q_organization_job_titles`` (companies currently hiring the role) and
   ``contact_email_status: verified`` — the same hard rule
   apollo_query_builder.py:52 applies, so every suggestion is someone we can
   actually email.

Suggestions are advisory. Nothing is drafted, redirected or sent on the
student's behalf; they choose.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

APOLLO_COMPANY_URL = "https://api.apollo.io/api/v1/mixed_companies/search"
APOLLO_PEOPLE_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"

# How many alternatives to offer. Three is enough to feel like a real choice
# and few enough that the student reads all of them.
MAX_SUGGESTIONS = 3


def _company_profile(company: str, location: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Industry, tags and size of the company the student actually clicked.

    Returns None when Apollo does not know the company — in which case we have
    nothing to match on and should say so rather than suggest at random.
    """
    from services.shared.apollo_key_manager import apollo_post

    payload: Dict[str, Any] = {"q_organization_name": company, "per_page": 1, "page": 1}
    if location:
        payload["organization_locations"] = [location]

    try:
        resp = apollo_post(APOLLO_COMPANY_URL, json=payload, timeout=15)
    except Exception as e:
        logger.warning("[SIMILAR] company lookup failed for %s: %s", company, e)
        return None

    if not resp.ok:
        logger.warning("[SIMILAR] company lookup HTTP %d for %s", resp.status_code, company)
        return None

    try:
        data = resp.json()
    except Exception:
        return None

    # Apollo answers 200 with the error in the body when a key is exhausted.
    if isinstance(data, dict) and data.get("error"):
        logger.error("[SIMILAR] Apollo refused the company lookup: %s", str(data["error"])[:160])
        return None

    orgs = (data.get("organizations") or data.get("accounts") or []) if isinstance(data, dict) else []
    if not orgs:
        logger.info("[SIMILAR] Apollo does not know %s — nothing to match on", company[:60])
        return None

    top = orgs[0]
    size = top.get("estimated_num_employees")
    profile = {
        "name": top.get("name") or company,
        "industry": top.get("industry"),
        # Niche tags are what make "fintech trading" different from "fintech
        # lending". Apollo caps these at 3 in a query.
        "keywords": [k for k in (top.get("keywords") or [])[:3] if k],
        "size_range": _size_band(size),
        "location": location,
    }
    logger.info(
        "[SIMILAR] profile of %s: industry=%s size=%s tags=%s",
        company[:40], profile["industry"], profile["size_range"], profile["keywords"],
    )
    return profile


def _size_band(headcount: Optional[int]) -> Optional[str]:
    """Apollo's own headcount bands.

    Matched to a BAND rather than an exact number: a student who applied to a
    30-person startup wants other startups, not a 30,000-person bank.
    """
    if not headcount:
        return None
    for lo, hi, band in (
        (1, 10, "1,10"), (11, 20, "11,20"), (21, 50, "21,50"),
        (51, 100, "51,100"), (101, 200, "101,200"), (201, 500, "201,500"),
        (501, 1000, "501,1000"), (1001, 5000, "1001,5000"),
    ):
        if lo <= headcount <= hi:
            return band
    return "5001,10000"


def find_similar_companies(
    company: str,
    role: Optional[str] = None,
    location: Optional[str] = None,
    limit: int = MAX_SUGGESTIONS,
) -> List[Dict[str, Any]]:
    """Companies like ``company``, hiring like ``role``, that we can reach.

    Every result has a named contact with a VERIFIED email — an alternative we
    cannot email is the same dead end we are trying to escape.

    Returns [] rather than raising: this runs on a page the student is already
    reading, and a failure here must leave that page working.
    """
    from services.shared.apollo_key_manager import apollo_post
    from services.extension.contact_finder import HIRING_TITLES, _same_company, _score_title

    profile = _company_profile(company, location)
    if not profile:
        return []

    payload: Dict[str, Any] = {
        "person_titles": HIRING_TITLES,
        # The hard rule from apollo_query_builder.py:52. Without it we would
        # suggest companies we cannot email either.
        "contact_email_status": ["verified"],
        "per_page": 25,
        "page": 1,
    }
    if profile["industry"]:
        payload["organization_industries"] = [profile["industry"]]
    if profile["keywords"]:
        payload["q_organization_keyword_tags"] = profile["keywords"]
    if profile["size_range"]:
        payload["organization_num_employees_ranges"] = [profile["size_range"]]
    if location:
        payload["organization_locations"] = [location]
    # Companies currently hiring this role — the field the leads page uses for
    # exactly this purpose (filter_schema.py:4).
    if role:
        payload["q_organization_job_titles"] = [role]

    try:
        resp = apollo_post(APOLLO_PEOPLE_URL, json=payload, timeout=20)
    except Exception as e:
        logger.warning("[SIMILAR] people search failed for %s: %s", company, e)
        return []

    if not resp.ok:
        logger.warning("[SIMILAR] people search HTTP %d for %s", resp.status_code, company)
        return []

    try:
        data = resp.json()
    except Exception:
        return []

    if isinstance(data, dict) and data.get("error"):
        logger.error("[SIMILAR] Apollo refused the people search: %s", str(data["error"])[:160])
        return []

    people = (data.get("people") or []) if isinstance(data, dict) else []

    # One suggestion per company, best contact first.
    by_company: Dict[str, Dict[str, Any]] = {}
    for p in people:
        first = (p.get("first_name") or "").strip()
        if not first:
            continue
        org = p.get("organization") or {}
        org_name = (org.get("name") or p.get("organization_name") or "").strip()
        if not org_name:
            continue
        # Never suggest the company they already tried.
        if _same_company(company, org_name):
            continue

        title = (p.get("title") or "").strip()
        score = _score_title(title)
        existing = by_company.get(org_name.lower())
        if existing and existing["score"] >= score:
            continue

        by_company[org_name.lower()] = {
            "company": org_name,
            "contact_name": f"{first} {(p.get('last_name') or '').strip()}".strip(),
            "contact_title": title,
            "apollo_id": (p.get("id") or "").strip() or None,
            "linkedin_url": (p.get("linkedin_url") or "").strip() or None,
            "industry": org.get("industry") or profile["industry"],
            "score": score,
        }

    out = sorted(by_company.values(), key=lambda c: c["score"], reverse=True)[:limit]
    logger.info(
        "[SIMILAR] company=%s role=%s suggested=%d from=%d people",
        company[:40], (role or "")[:40], len(out), len(people),
    )
    return out
