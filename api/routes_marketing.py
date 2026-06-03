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


def _normalise(s: str) -> str:
    return (s or "").strip()


@router.post("/find-lead")
async def find_lead(body: FindLeadRequest):
    company = _normalise(body.company)
    position = _normalise(body.position)
    if not company:
        raise HTTPException(status_code=400, detail="Company name is required")
    if not position:
        raise HTTPException(status_code=400, detail="Position is required")

    # 1) Find ONE person at <company> with title matching <position>.
    #    api_search returns first_name, title, and org.name in the clear; the
    #    last name is obfuscated as "Da***a" and LinkedIn/email are hidden
    #    behind /people/match (which costs 1 credit).
    try:
        resp = apollo_post(
            APOLLO_SEARCH_URL,
            json={
                "q_organization_name": company,
                "person_titles": [position],
                "per_page": 5,
                "page": 1,
            },
            timeout=30,
        )
    except Exception as e:
        logger.error("[MARKETING] Apollo search failed: %s", e)
        raise HTTPException(status_code=502, detail="Search service is unavailable. Try again in a moment.")

    if resp.status_code != 200:
        logger.warning("[MARKETING] Apollo search %d: %s", resp.status_code, resp.text[:200])
        raise HTTPException(status_code=502, detail="Search failed. Try a different company or role.")

    data = resp.json() or {}
    people = data.get("people", []) or []

    lead = None
    company_lower = company.lower()
    for p in people:
        first = (p.get("first_name") or "").strip()
        if not first:
            continue
        org = p.get("organization") or {}
        org_name = (org.get("name") or p.get("organization_name") or "").strip()
        if not org_name:
            continue
        # Loose match — Apollo sometimes returns "Razorpay Inc" for "Razorpay"
        if company_lower not in org_name.lower() and org_name.lower() not in company_lower:
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
        return {"lead": None, "similar_companies": []}

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

    return {"lead": lead, "similar_companies": similar_companies}


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
