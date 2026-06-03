"""Marketing Dojo — quick single-lead lookup tool.

Spin-off product separate from the main outreach pipeline. Given a company name
and a target position, returns ONE hiring manager (name, title, LinkedIn URL)
plus a list of similar companies — all without burning Apollo credits.

Apollo credits ARE burned only on the explicit /enrich-email endpoint, which
requires Studojo auth so we can attribute usage and prevent anonymous abuse.

Endpoints:
  POST /marketing/find-lead     (public)  — name + title + LinkedIn (free)
  POST /marketing/enrich-email  (auth)    — reveal email (1 credit, audited)
"""

import logging
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from api.dependencies import get_current_user
from database.models import User
from services.shared.apollo_key_manager import apollo_post

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/marketing", tags=["Marketing Dojo"])


# Apollo has two people-search endpoints:
#   /mixed_people/search    — UI/preview, charges credits on most plans
#   /mixed_people/api_search — free, but returns OBFUSCATED last names and
#                              omits LinkedIn URL / city / email (all the
#                              good stuff lives behind /people/match)
# We use api_search for free discovery, then /people/match (1 credit) to
# reveal the contact when the user explicitly clicks Enrich.
APOLLO_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
APOLLO_MATCH_URL = "https://api.apollo.io/api/v1/people/match"


# ── Find lead (free — no credit burn) ─────────────────────────────────────────


class FindLeadRequest(BaseModel):
    company: str
    position: str
    location: Optional[str] = None  # optional — e.g. "India", "San Francisco", "London"


def _normalise(s: str) -> str:
    return (s or "").strip()


# Map common exec/role keywords → Apollo seniority codes. Apollo's
# `person_titles` filter is an EXACT-substring match; "ceo" misses the real
# CEO of Bain whose title is "Worldwide Managing Partner". For exec-level
# queries we fall back to a seniority filter that catches all such titles.
_SENIORITY_BY_KEYWORD = [
    # (keyword that appears in user input, apollo seniority code)
    ("ceo", "c_suite"),
    ("chief executive", "c_suite"),
    ("cto", "c_suite"),
    ("cfo", "c_suite"),
    ("coo", "c_suite"),
    ("cmo", "c_suite"),
    ("cpo", "c_suite"),
    ("chro", "c_suite"),
    ("ciso", "c_suite"),
    ("president", "c_suite"),
    ("founder", "founder"),
    ("co-founder", "founder"),
    ("cofounder", "founder"),
    ("managing partner", "partner"),
    ("partner", "partner"),
    ("svp", "vp"),
    ("evp", "vp"),
    ("vp", "vp"),
    ("vice president", "vp"),
    ("head of", "head"),
    ("director", "director"),
    ("manager", "manager"),
]


def _infer_seniority(position: str) -> str | None:
    p = position.lower()
    for kw, sen in _SENIORITY_BY_KEYWORD:
        if kw in p:
            return sen
    return None


def _apollo_call(payload: dict) -> list:
    payload.setdefault("per_page", 5)
    payload.setdefault("page", 1)
    resp = apollo_post(APOLLO_SEARCH_URL, json=payload, timeout=30)
    if resp.status_code != 200:
        logger.warning("[MARKETING] Apollo search %d: %s", resp.status_code, resp.text[:200])
        raise HTTPException(status_code=502, detail="Search failed. Try a different company or role.")
    return (resp.json() or {}).get("people", []) or []


def _org_variants(name: str) -> list[str]:
    """Return ordered variants of an org name to try in cascade.

    Apollo's canonical records use "&" not " and " ("Bain & Company"), so
    when the user types the spelled-out form we retry with the symbol. We
    deliberately don't strip suffix words like "Inc" because Apollo's
    q_organization_name already does substring matching ("razorpay" matches
    "Razorpay Inc"), and stripping creates ugly fragments ("bain &").
    """
    n = (name or "").strip()
    if not n:
        return []
    variants = [n]
    if re.search(r"\band\b", n, re.IGNORECASE):
        variants.append(re.sub(r"\band\b", "&", n, flags=re.IGNORECASE).strip())
    # Dedupe preserving order
    seen, out = set(), []
    for v in variants:
        k = v.lower()
        if k not in seen:
            seen.add(k)
            out.append(v)
    return out


def _do_apollo_search(company: str, position: str, location: Optional[str] = None) -> list:
    """Cascade through org name variants × (exact title → seniority fallback).

    Stops at the first variant that returns results. Each Apollo call is
    free on the api_search endpoint so the worst case (4 calls) costs us
    only latency, not credits.

    Location is optional. When supplied, narrows by person_locations on the
    first pass; if that returns nothing we drop the location filter and try
    again so the user always gets *some* answer rather than a brick wall.
    """
    seniority = _infer_seniority(position)
    loc_filter: dict = {"person_locations": [location]} if location else {}

    def _try(org: str, with_location: bool) -> list:
        people: list = []
        loc = loc_filter if with_location else {}
        # Tier 1 — exact title
        people = _apollo_call({"q_organization_name": org, "person_titles": [position], **loc})
        if people:
            logger.info(
                "[MARKETING] Hit via title at org=%r (location=%s)",
                org, location if with_location else "off",
            )
            return people
        # Tier 2 — seniority fallback
        if seniority:
            people = _apollo_call({"q_organization_name": org, "person_seniorities": [seniority], **loc})
            if people:
                logger.info(
                    "[MARKETING] Hit via seniority=%s at org=%r (location=%s)",
                    seniority, org, location if with_location else "off",
                )
                return people
        return []

    for org in _org_variants(company):
        # First pass — with location filter (if provided)
        if location:
            people = _try(org, with_location=True)
            if people:
                return people
        # Second pass — without location, in case Apollo doesn't tag this person
        people = _try(org, with_location=False)
        if people:
            return people
    return []


@router.post("/find-lead")
async def find_lead(body: FindLeadRequest):
    company = _normalise(body.company)
    position = _normalise(body.position)
    location = _normalise(body.location or "") or None
    if not company:
        raise HTTPException(status_code=400, detail="Company name is required")
    if not position:
        raise HTTPException(status_code=400, detail="Position is required")

    # 1) Find ONE person at <company> with title matching <position>.
    #    api_search returns first_name, title, and org.name in the clear; the
    #    last name is obfuscated as "Da***a" and LinkedIn/email are hidden
    #    behind /people/match (which costs 1 credit).
    try:
        people = _do_apollo_search(company, position, location)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[MARKETING] Apollo search failed: %s", e)
        raise HTTPException(status_code=502, detail="Search service is unavailable. Try again in a moment.")

    swapped_used = False
    if not people:
        # Auto-swap fallback: maybe the user filled the fields backwards
        # ("analyst" in Company, "bain" in Position). Try the inverted query.
        try:
            swapped_people = _do_apollo_search(position, company, location)
            if swapped_people:
                logger.info(
                    "[MARKETING] Empty result for %r at %r — succeeded with swap: %r at %r",
                    position, company, company, position,
                )
                people = swapped_people
                company, position = position, company
                swapped_used = True
        except Exception:
            pass


    # Tokenise the query company for forgiving match against Apollo's canonical
    # name. "bain and company" → {"bain", "company"}; "Bain & Company" → {"bain", "company"}.
    # We accept the lead if any significant token overlaps.
    _stop = {"and", "&", "the", "of", "inc", "llc", "ltd", "limited", "corp",
             "corporation", "company", "co", "pvt", "gmbh"}
    def _tokens(s: str) -> set[str]:
        return {t for t in re.split(r"[^a-z0-9]+", s.lower()) if t and t not in _stop}

    lead = None
    company_lower = company.lower()
    company_tokens = _tokens(company)
    for p in people:
        first = (p.get("first_name") or "").strip()
        if not first:
            continue
        org = p.get("organization") or {}
        org_name = (org.get("name") or p.get("organization_name") or "").strip()
        if not org_name:
            continue
        org_lower = org_name.lower()
        # Accept if either is a substring OR if significant tokens overlap
        # (handles "bain and company" ↔ "Bain & Company", "Razorpay" ↔ "Razorpay Inc")
        substring_match = company_lower in org_lower or org_lower in company_lower
        token_match = bool(company_tokens & _tokens(org_name))
        if not (substring_match or token_match):
            continue
        last_obf = (p.get("last_name_obfuscated") or "").strip()
        lead = {
            "apollo_person_id": p.get("id"),
            "first_name": first,
            "last_name_obfuscated": last_obf,  # e.g. "Da***a" — full name on enrich
            "display_name": f"{first} {last_obf}".strip() if last_obf else first,
            "title": p.get("title") or "",
            "company": org_name,
            "has_email": bool(p.get("has_email")),
            "last_refreshed_at": p.get("last_refreshed_at"),
        }
        break

    if not lead:
        return {
            "lead": None,
            "similar_companies": [],
            "searched_company": company,
            "searched_position": position,
            "swapped": swapped_used,
        }

    # 2) Similar companies — broader api_search with the same role but no
    #    company filter. Collect distinct organisation names from the results.
    #    Free; uses the same endpoint we already know works on this plan.
    similar_companies: list[dict] = []
    try:
        sim_resp = apollo_post(
            APOLLO_SEARCH_URL,
            json={
                "person_titles": [position],
                "per_page": 25,
                "page": 1,
            },
            timeout=20,
        )
        if sim_resp.status_code == 200:
            seen_lower = {company_lower, (lead.get("company") or "").lower()}
            for p in (sim_resp.json() or {}).get("people") or []:
                org = p.get("organization") or {}
                name = (org.get("name") or "").strip()
                if not name or name.lower() in seen_lower:
                    continue
                seen_lower.add(name.lower())
                similar_companies.append({"name": name, "domain": None, "logo_url": None, "industry": None})
                if len(similar_companies) >= 6:
                    break
    except Exception as e:
        logger.warning("[MARKETING] Similar-companies lookup failed (non-fatal): %s", e)

    return {
        "lead": lead,
        "similar_companies": similar_companies,
        "searched_company": company,
        "searched_position": position,
        "swapped": swapped_used,
    }


# ── Enrich email (BURNS 1 Apollo credit — requires auth) ───────────────────────


class EnrichEmailRequest(BaseModel):
    apollo_person_id: Optional[str] = None
    # Allow enrichment by name+company when person ID isn't known
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    linkedin_url: Optional[str] = None


@router.post("/enrich-email")
async def enrich_email(
    body: EnrichEmailRequest,
    current_user: User = Depends(get_current_user),
):
    """Reveal a verified email via Apollo /people/match.

    Burns exactly ONE Apollo credit per successful call. Auth required so each
    burn is attributable. Logs every attempt for audit.
    """
    payload: dict = {}
    if body.apollo_person_id:
        payload["id"] = body.apollo_person_id
    elif body.linkedin_url:
        payload["linkedin_url"] = body.linkedin_url
    elif body.first_name and body.company:
        payload["first_name"] = body.first_name
        if body.last_name:
            payload["last_name"] = body.last_name
        payload["organization_name"] = body.company
    else:
        raise HTTPException(
            status_code=400,
            detail="Provide apollo_person_id, linkedin_url, or first_name+company.",
        )

    # Required reveal flag — without this Apollo returns the record but NO email.
    payload["reveal_personal_emails"] = False  # work email only
    payload["reveal_phone_number"] = False

    logger.info(
        "[MARKETING-ENRICH] user=%s payload_keys=%s (CREDIT BURN ATTEMPT)",
        current_user.id, list(payload.keys()),
    )

    try:
        resp = apollo_post(APOLLO_MATCH_URL, json=payload, timeout=30)
    except Exception as e:
        logger.error("[MARKETING-ENRICH] Apollo error: %s", e)
        raise HTTPException(status_code=502, detail="Enrichment service unavailable. No credit was used.")

    if resp.status_code != 200:
        logger.warning(
            "[MARKETING-ENRICH] Apollo /people/match %d: %s",
            resp.status_code, resp.text[:200],
        )
        raise HTTPException(
            status_code=502,
            detail="Could not enrich this contact. Apollo returned an error — no credit charged.",
        )

    data = resp.json() or {}
    person = data.get("person") or {}
    email = person.get("email")
    email_status = person.get("email_status")  # "verified" / "guessed" / "unavailable"

    logger.info(
        "[MARKETING-ENRICH] user=%s person=%s email_status=%s email_found=%s",
        current_user.id, person.get("id"), email_status, bool(email),
    )

    return {
        "email": email,
        "email_status": email_status,
        "name": f"{person.get('first_name','')} {person.get('last_name','')}".strip(),
        "title": person.get("title"),
        "linkedin_url": person.get("linkedin_url"),
        "company": (person.get("organization") or {}).get("name"),
    }
