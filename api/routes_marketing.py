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
from sqlalchemy.orm import Session

from api.dependencies import get_current_user, get_db
from database.models import User
from services.shared.apollo_key_manager import apollo_post

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/marketing", tags=["Marketing Dojo"])


APOLLO_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/search"
APOLLO_COMPANY_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_companies/search"
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
    #    mixed_people/search is the free endpoint — returns name/title/linkedin
    #    but NOT the email (which would require /people/match → 1 credit).
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

    # Pick the best match — the first person who has both a name and looks
    # like they actually work at the requested company.
    lead = None
    company_lower = company.lower()
    for p in people:
        first = (p.get("first_name") or "").strip()
        last = (p.get("last_name") or "").strip()
        if not first:
            continue
        org = p.get("organization") or {}
        org_name = (org.get("name") or p.get("organization_name") or "").strip()
        if not org_name:
            continue
        # Loose match — Apollo sometimes returns "Razorpay Inc" for "Razorpay"
        if company_lower not in org_name.lower() and org_name.lower() not in company_lower:
            continue
        lead = {
            "apollo_person_id": p.get("id"),
            "name": f"{first} {last}".strip(),
            "first_name": first,
            "last_name": last,
            "title": p.get("title") or "",
            "linkedin_url": p.get("linkedin_url"),
            "headline": p.get("headline") or "",
            "company": org_name,
            "company_domain": org.get("primary_domain") or org.get("website_url"),
            "company_logo_url": org.get("logo_url"),
            "city": p.get("city"),
            "country": p.get("country"),
        }
        break

    if not lead:
        return {"lead": None, "similar_companies": []}

    # 2) Suggest similar companies — same industry/keyword tags as the found
    #    company. Also free; no credit burn.
    similar_companies: list[dict] = []
    try:
        org = (people[0] or {}).get("organization") or {}
        industry = org.get("industry")
        keywords = org.get("keywords") or []
        # Build a similarity query — industry first, fall back to top keyword
        sim_payload: dict = {"per_page": 6, "page": 1}
        if industry:
            sim_payload["q_organization_keyword_tags"] = [industry]
        elif keywords:
            sim_payload["q_organization_keyword_tags"] = [keywords[0]]
        if sim_payload.get("q_organization_keyword_tags"):
            sim_resp = apollo_post(APOLLO_COMPANY_SEARCH_URL, json=sim_payload, timeout=20)
            if sim_resp.status_code == 200:
                orgs = (sim_resp.json() or {}).get("organizations") or (sim_resp.json() or {}).get("accounts") or []
                seen_lower = {company_lower, (lead.get("company") or "").lower()}
                for o in orgs:
                    name = (o.get("name") or "").strip()
                    if not name or name.lower() in seen_lower:
                        continue
                    seen_lower.add(name.lower())
                    similar_companies.append({
                        "name": name,
                        "domain": o.get("primary_domain") or o.get("website_url"),
                        "logo_url": o.get("logo_url"),
                        "industry": o.get("industry"),
                    })
                    if len(similar_companies) >= 5:
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
    db: Session = Depends(get_db),
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
