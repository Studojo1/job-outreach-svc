"""Find someone to write to when the job page didn't name anyone.

The problem this solves
-----------------------
A job page names a person maybe half the time. Before this, the other half was
a dead end: the student got a draft they could edit and could never send,
because there was no recipient. The product's whole premise is "email a real
person instead of applying into a void" — and it was falling back to the void
whenever the page happened not to name someone.

So: if the page gave us a name, use it. If it did not, go and find the people
who would actually be hiring for that role at that company.

How
---
Apollo's ``mixed_people/api_search`` takes ``q_organization_name`` plus
``person_titles``. That is the same endpoint the LinkedIn automation flow
already uses, so this is a new query, not a new integration.

Titles are ranked by who is actually likely to reply to a student about a
specific opening. A recruiter beats a VP: they own the requisition, they read
their inbox, and a cold note from a candidate is a normal part of their day.
A founder is last — right about them being reachable at a small company, wrong
about them being the best first ask at a large one.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

APOLLO_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"

# Ordered by who is most likely to read and answer a student's note about a
# specific role. Searched as one query — Apollo ranks within the set, and we
# re-rank by this order afterwards.
HIRING_TITLES: List[str] = [
    "Technical Recruiter",
    "Recruiter",
    "Talent Acquisition",
    "Talent Acquisition Specialist",
    "Head of Talent",
    "HR Manager",
    "Human Resources",
    "Hiring Manager",
    "Engineering Manager",
    "Head of Engineering",
    "VP Engineering",
    "Director of Engineering",
    "Founder",
    "Co-Founder",
    "CTO",
]

# How strongly to prefer each title, highest first. Anything unlisted scores 0
# and can still be used, but only if nothing better came back.
_TITLE_RANK: Dict[str, int] = {
    "recruit": 100,
    "talent": 95,
    "human resources": 85,
    "hr ": 85,
    "hiring": 80,
    "people ops": 75,
    "engineering manager": 60,
    "head of engineering": 55,
    "director": 50,
    "vp ": 45,
    "founder": 30,
    "cto": 25,
    "chief": 20,
}


def _score_title(title: str) -> int:
    t = (title or "").strip().lower()
    if not t:
        return 0
    for needle, score in _TITLE_RANK.items():
        if needle in t:
            return score
    return 10  # a real person at the company still beats nobody


def find_hiring_contacts(
    company: str,
    role: Optional[str] = None,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """People at ``company`` who plausibly hire for ``role``.

    Returns dicts shaped like the extension's own contact payload — name,
    title, linkedin_url, email — best first. Returns [] rather than raising:
    a failure here must leave the student exactly where they were, not break
    the page.

    NOTE: this is a search, not a reveal. It costs an Apollo API call but does
    NOT burn a reveal credit — that happens later, only when we resolve the
    chosen person's email address.
    """
    company = (company or "").strip()
    if not company:
        return []

    from services.shared.apollo_key_manager import apollo_post

    payload: Dict[str, Any] = {
        "q_organization_name": company,
        "person_titles": HIRING_TITLES,
        "per_page": max(limit * 3, 15),
        "page": 1,
    }

    try:
        resp = apollo_post(APOLLO_SEARCH_URL, json=payload, timeout=20)
    except Exception as e:
        logger.warning("[CONTACT-FIND] Apollo search failed for %s: %s", company, e)
        return []

    if not resp.ok:
        logger.warning(
            "[CONTACT-FIND] Apollo returned %d for %s: %s",
            resp.status_code, company, resp.text[:200],
        )
        return []

    try:
        people = resp.json().get("people", []) or []
    except Exception:
        logger.warning("[CONTACT-FIND] Apollo sent a body we could not read for %s", company)
        return []

    out: List[Dict[str, Any]] = []
    for p in people:
        first = (p.get("first_name") or "").strip()
        last = (p.get("last_name") or "").strip()
        name = f"{first} {last}".strip()
        # A name is the minimum: "Hi there" is what we are trying to escape.
        if not first:
            continue

        org = p.get("organization") or {}
        org_name = (org.get("name") or p.get("organization_name") or "").strip()
        # Apollo matches loosely on company. Reject anyone who is not actually
        # at the company the student applied to — emailing a stranger at a
        # similarly-named firm is worse than finding nobody.
        if org_name and company.lower() not in org_name.lower() and org_name.lower() not in company.lower():
            continue

        title = (p.get("title") or "").strip()
        out.append({
            "name": name,
            "title": title,
            "company": org_name or company,
            "linkedin_url": (p.get("linkedin_url") or "").strip() or None,
            # Present only when Apollo already holds it unlocked; otherwise the
            # normal enrichment path resolves it when the student sends.
            "email": (p.get("email") or "").strip() or None,
            "score": _score_title(title),
        })

    out.sort(key=lambda c: c["score"], reverse=True)
    logger.info(
        "[CONTACT-FIND] company=%s role=%s found=%d best=%s",
        company[:60], (role or "")[:40], len(out),
        (out[0]["title"][:40] if out else "-"),
    )
    return out[:limit]
