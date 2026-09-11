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
    # Recruiting and people — they own the requisition whatever the role is.
    "Technical Recruiter",
    "Recruiter",
    "Senior Recruiter",
    "Talent Acquisition",
    "Talent Acquisition Specialist",
    "Talent Partner",
    "Head of Talent",
    "Head of People",
    "People Operations",
    "People Partner",
    "HR Manager",
    "HR Business Partner",
    "Human Resources",
    "Hiring Manager",
    "Campus Recruiter",          # the one that matters for an internship
    "University Recruiter",
    # Function leads. The previous list was engineering-only, which found
    # nobody for a Product Operations or Analytics opening — the exact role
    # that failed at Sarvam.
    "Engineering Manager",
    "Head of Engineering",
    "VP Engineering",
    "Director of Engineering",
    "Product Manager",
    "Head of Product",
    "Director of Product",
    "Head of Operations",
    "Operations Manager",
    "Head of Analytics",
    "Data Science Manager",
    "Chief of Staff",            # common owner of hiring at Indian startups
    # Last resort — right at a 10-person company, wrong at a large one.
    "Founder",
    "Co-Founder",
    "CTO",
]

# How strongly to prefer each title, highest first. Anything unlisted scores 0
# and can still be used, but only if nothing better came back.
# ORDER MATTERS: the first substring hit wins. "campus recruiter" contains
# "recruit", so the student-specific entries must come first or they never
# score above a generic recruiter.
_TITLE_RANK: Dict[str, int] = {
    "campus recruit": 110,
    "university recruit": 110,
    "recruit": 100,
    "talent": 95,
    "human resources": 85,
    "hr ": 85,
    "hiring": 80,
    "people ops": 75,
    "engineering manager": 60,
    "head of engineering": 55,
    "chief of staff": 58,
    "head of product": 55,
    "product manager": 48,
    "head of operations": 55,
    "operations manager": 48,
    "head of analytics": 55,
    "data science": 45,
    "director": 50,
    "vp ": 45,
    "founder": 30,
    "cto": 25,
    "chief": 20,
}


_LEGAL_SUFFIXES = (
    "private limited", "pvt ltd", "pvt", "private", "limited", "ltd",
    "incorporated", "inc", "corporation", "corp", "llc", "llp", "plc", "gmbh",
    "technologies", "technology", "labs", "software", "solutions", "systems",
    "india", "global", "group", "holdings", "co",
)


def _normalise_company(name: str) -> str:
    """A company name reduced to its distinctive core.

    "Pipraiser Technologies Pvt Ltd" and "Pipraiser" must compare equal, or a
    real match is thrown away and the student is told nobody works there.
    """
    import re

    n = (name or "").lower()
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    words = [w for w in n.split() if w]
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    joined = " ".join(words)
    for suffix in ("private limited", "pvt ltd"):
        if joined.endswith(suffix):
            joined = joined[: -len(suffix)].strip()
    return joined or " ".join(w for w in n.split() if w)


def _same_company(a: str, b: str) -> bool:
    """Do these two names refer to the same company?

    WHOLE WORDS, never a raw substring: "Stripe" is not "Striped Analytics"
    and "Meta" is not "Metabase". A substring test matched both and would have
    emailed a stranger at a company the student never applied to.
    """
    na, nb = _normalise_company(a), _normalise_company(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    wa, wb = na.split(), nb.split()
    if wa[0] == wb[0] and len(wa[0]) >= 3:
        return True
    shorter, longer = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    return any(longer[i : i + len(shorter)] == shorter for i in range(len(longer) - len(shorter) + 1))


# Free public mail domains. An address at one of these tells us nothing about
# where the person works, so it can never confirm a company match.
_PUBLIC_MAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "outlook.com",
    "hotmail.com", "live.com", "icloud.com", "proton.me", "protonmail.com",
    "rediffmail.com", "aol.com", "zoho.com", "mail.com",
}


def email_matches_company(email: str, company: str) -> bool:
    """Does this address plausibly belong to someone AT this company?

    THE REVEAL DOES NOT CHECK THIS, AND CANNOT.

    Apollo's people/match returns ``organization: None`` on every call —
    verified against Razorpay, Fractal, Zerodha and Swiggy — so there is no
    organisation field to compare. It matches largely on NAME, and marks the
    result ``email_status: "verified"`` regardless of employer.

    Searching for "Sumit Kumar at Razorpay" returned
    ``sumit@razorcapital.net`` — Razor Capital, a different company — and our
    only gate was the verified flag, which says the ADDRESS is real, not that
    it is the RIGHT PERSON. A student would have emailed a stranger at a
    company they never applied to. That is worse than finding nobody.

    So the domain is the evidence we have. A public mailbox proves nothing
    either way and is rejected: we cannot confirm it, and confirming is the
    whole point.
    """
    addr = (email or "").strip().lower()
    if "@" not in addr:
        return False
    domain = addr.rsplit("@", 1)[1]
    if not domain or domain in _PUBLIC_MAIL:
        return False

    # Compare the domain's distinctive part against the normalised company.
    host = domain.rsplit(".", 1)[0]           # razorcapital.net -> razorcapital
    host = host.rsplit(".", 1)[-1]            # mail.acme.co.uk  -> acme
    target = _normalise_company(company).replace(" ", "")
    if not target:
        return False
    host_c = "".join(ch for ch in host if ch.isalnum())
    if not host_c:
        return False
    # Strip the legal//generic suffixes a domain tacks on, the same ones
    # _normalise_company removes from the name: acmecorp.com -> acme.
    for suffix in ("technologies", "technology", "solutions", "systems",
                   "software", "labs", "group", "global", "india",
                   "corp", "inc", "llc", "ltd", "hq", "co"):
        if host_c.endswith(suffix) and len(host_c) > len(suffix) + 2:
            host_c = host_c[: -len(suffix)]
            break
    # EXACT, or the domain extends the company name ("acme" -> "acmecorp").
    # NOT bare containment: "stripe" is inside "stripedanalytics", so a
    # containment test accepted q@stripe.com for "Striped Analytics" — the
    # same substring trap that matched Meta to Metabase.
    if host_c == target:
        return True
    if host_c.startswith(target) or target.startswith(host_c):
        # Guard against a short prefix matching by accident: require the
        # shorter side to be a substantial part of the longer one.
        shorter, longer = sorted((host_c, target), key=len)
        return len(shorter) >= 4 and len(shorter) / len(longer) >= 0.6
    return False


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
        # THE FIX. Without this Apollo returns people whose addresses it cannot
        # verify — so every reveal came back no_match and the page honestly
        # reported "we haven't found a confirmed email address", having
        # searched a set that could never contain one.
        #
        # The lead-discovery flow has always sent it, with the comment: "leads
        # without verified emails cannot be enriched and waste enrichment
        # credits. This is a hard rule." (apollo_query_builder.py:50-52). I
        # wrote this search without reading theirs.
        #
        # It also stops us paying for reveals that were always going to fail.
        "contact_email_status": ["verified"],
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
        data = resp.json()
    except Exception:
        logger.warning("[CONTACT-FIND] Apollo sent a body we could not read for %s", company)
        return []

    # Apollo returns HTTP 200 with the error INSIDE the body when a key is
    # exhausted or the plan does not permit the call. Without this check a
    # refused request is indistinguishable from "this company has nobody":
    # resp.ok is True, `people` is absent, and we report a dead end. The
    # enrichment path has always checked for it (enrichment_service.py:209-213);
    # this search never did, which is why four rounds of fixes could not tell
    # the two apart.
    body_error = (data.get("error") or "") if isinstance(data, dict) else ""
    if body_error:
        logger.error(
            "[CONTACT-FIND] Apollo REFUSED the search for %s (HTTP 200, body error): %s",
            company, str(body_error)[:200],
        )
        if any(
            phrase in str(body_error).lower()
            for phrase in ("insufficient credits", "not accessible", "upgrade your plan", "credit limit")
        ):
            from services.shared.apollo_key_manager import apollo_keys
            current = apollo_keys.get_key()
            if current:
                apollo_keys.report_failure(current, 402)
        return []

    people = data.get("people", []) or []

    # total_entries separates "Apollo holds nobody matching" from "this page
    # returned nothing". The working discovery flow logs it
    # (apollo_service.py:56-58); not having it is why every failure looked
    # identical from the outside.
    total = 0
    if isinstance(data, dict):
        total = data.get("pagination", {}).get("total_entries", data.get("total_entries", 0)) or 0
    logger.info(
        "[CONTACT-FIND] apollo company=%s returned=%d total_available=%d titles=%d verified_only=True",
        company[:60], len(people), total, len(HIRING_TITLES),
    )

    # Nothing came back WITH the verified filter. Probe once without it, so the
    # logs distinguish "Apollo has no verified contacts here" from "Apollo has
    # nobody here" — the first is a coverage limit we can work around, the
    # second is not, and telling them apart decides whether this works for
    # small companies at all.
    if not people:
        try:
            probe = dict(payload)
            probe.pop("contact_email_status", None)
            probe["per_page"] = 5
            probe_resp = apollo_post(APOLLO_SEARCH_URL, json=probe, timeout=15)
            if probe_resp.ok:
                probe_people = (probe_resp.json() or {}).get("people", []) or []
                logger.info(
                    "[CONTACT-FIND] probe company=%s unverified_matches=%d "
                    "(0 = Apollo has nobody; >0 = people exist but no verified email)",
                    company[:60], len(probe_people),
                )
        except Exception as e:
            logger.debug("[CONTACT-FIND] probe failed for %s: %s", company, e)

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
        # Compare on a NORMALISED name, not a raw substring. A job board says
        # "Pipraiser" while Apollo holds "Pipraiser Technologies Pvt Ltd" —
        # once the suffixes differ neither string contains the other, so every
        # legitimate result was dropped and the page reported nobody there.
        if org_name and not _same_company(company, org_name):
            continue

        title = (p.get("title") or "").strip()
        out.append({
            # Apollo's own id for this person. The reveal passes it as the
            # match key (enrichment_service.py:179), and it is the strongest
            # one there is — without it we search Apollo, find someone, throw
            # away the exact identifier, and ask Apollo to guess them again
            # from a name and a company. The lead-discovery flow requires it
            # (lead_collector_service.py:248 drops anyone without an id).
            "apollo_id": (p.get("id") or "").strip() or None,
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
