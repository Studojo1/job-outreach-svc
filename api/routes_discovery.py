"""Discovery Routes — Lead discovery via Apollo."""

import asyncio
import logging
import time
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel

from database.session import get_db
from database.models import User, Candidate, Lead, LeadScore
from services.lead_discovery.lead_collector_service import collect_leads
from services.shared.schemas.filter_schema import LeadFilter
from services.shared.schemas.candidate_schema import CandidateProfile
from api.dependencies import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/discovery", tags=["Discovery"])


class DiscoveryRequest(BaseModel):
    candidate_id: int
    target_leads: int = 500
    filters: Optional[LeadFilter] = None


def _score_candidate_leads(db: Session, candidate: Candidate) -> int:
    """Score all unscored leads for a candidate and store LeadScore records."""
    parsed = candidate.parsed_json or {}
    career = parsed.get("career_analysis", {})
    prefs = parsed.get("preferences", {})

    # Build candidate profile dict for scorer
    preferred_roles = [r.get("title", "") for r in career.get("recommended_roles", []) if r.get("title")]
    if not preferred_roles:
        preferred_roles = candidate.target_roles or ["Software Engineer"]

    candidate_profile = {
        "preferred_roles": preferred_roles,
        "target_roles": candidate.target_roles or preferred_roles,
        "location_preferences": prefs.get("locations", []),
        "company_preferences": {
            "industries": prefs.get("industry_interests", []),
            "company_size": [prefs.get("company_size", "any")],
        },
    }

    # Determine candidate seniority from recommended roles
    role_seniorities = [r.get("seniority", "entry") for r in career.get("recommended_roles", [])]
    # Pick the most common seniority, default to "entry"
    candidate_seniority = "entry"
    if role_seniorities:
        from collections import Counter
        candidate_seniority = Counter(role_seniorities).most_common(1)[0][0]

    # Build role intelligence dict for scorer
    role_intelligence = {
        "hiring_roles": preferred_roles,
        "industry_expansion": prefs.get("industry_interests", []),
        "company_size_preferences": [prefs.get("company_size", "1,10000")],
        "departments": [career.get("primary_cluster", "").lower()],
        "target_seniorities": role_seniorities,
        "locations": prefs.get("locations", []),
        "candidate_seniority": candidate_seniority,
    }

    # Get all unscored leads
    leads = db.query(Lead).filter_by(candidate_id=candidate.id).all()
    scored_lead_ids = {s.lead_id for s in db.query(LeadScore.lead_id).filter(
        LeadScore.lead_id.in_([l.id for l in leads])
    ).all()} if leads else set()

    unscored = [l for l in leads if l.id not in scored_lead_ids]
    if not unscored:
        return 0

    # Convert to dicts for scoring service
    lead_dicts = []
    lead_id_map = {}
    for lead in unscored:
        d = {
            "id": lead.id,
            "apollo_person_id": lead.apollo_id,
            "name": lead.name,
            "title": lead.title,
            "company": lead.company,
            "industry": lead.industry,
            "location": lead.location,
            "company_size": lead.company_size,
            "linkedin_url": lead.linkedin_url,
        }
        lead_dicts.append(d)
        lead_id_map[lead.id] = lead

    from services.lead_scoring.lead_scoring_service import score_and_select_leads as score_leads_svc
    scored = score_leads_svc(
        leads=lead_dicts,
        candidate_profile=candidate_profile,
        role_intelligence=role_intelligence,
        target_count=len(lead_dicts),
    )

    # Store scores in DB using actual component scores from the scorer
    count = 0
    for lead_dict in scored:
        lead_id = lead_dict.get("id")
        if lead_id:
            ls = LeadScore(
                lead_id=lead_id,
                overall_score=lead_dict.get("score", 0),
                title_relevance=lead_dict.get("_title_score", 0),
                department_relevance=lead_dict.get("_dept_score", 0),
                industry_relevance=lead_dict.get("_industry_score", 0),
                seniority_relevance=lead_dict.get("_seniority_score", 0),
                location_relevance=lead_dict.get("_location_score", 0),
                explanation=f"Scored against {', '.join(preferred_roles[:3])} (seniority={candidate_seniority})",
            )
            db.add(ls)
            count += 1

    db.commit()
    return count


@router.post("/search")
async def search_leads(
    request: DiscoveryRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Execute lead discovery based on candidate profile."""
    t_start = time.perf_counter()
    # Hard minimum: always retrieve at least 500 leads for a usable pool
    if request.target_leads < 500:
        logger.info(f"[DISCOVERY] target_leads={request.target_leads} below minimum, enforcing 500")
        request.target_leads = 500
    logger.info(f"[DISCOVERY] POST /discovery/search — candidate_id={request.candidate_id}, target={request.target_leads}")

    candidate = db.query(Candidate).filter_by(
        id=request.candidate_id, user_id=current_user.id
    ).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    try:
        if request.filters:
            filters = request.filters
            logger.info("[LeadSearch] Using pre-built filters from request")
        else:
            # Convert parsed_json (LLM payload) into CandidateProfile for filter generation
            parsed = candidate.parsed_json or {}
            career = parsed.get("career_analysis", {})
            prefs = parsed.get("preferences", {})
            personal = parsed.get("personal_info", {})

            logger.info(f"[LeadSearch] Candidate profile loaded — has_career={bool(career)}, has_prefs={bool(prefs)}, has_personal={bool(personal)}")

            # Extract preferred roles from recommended_roles
            recommended_roles = career.get("recommended_roles", [])
            preferred_roles = [r.get("title", "") for r in recommended_roles if r.get("title")]
            if not preferred_roles:
                preferred_roles = candidate.target_roles or ["Software Engineer"]

            logger.info(f"[LeadSearch] Preferred roles: {preferred_roles}")
            logger.info(f"[LeadSearch] Locations: {prefs.get('locations', [])}")
            logger.info(f"[LeadSearch] Industries: {prefs.get('industry_interests', [])}")

            # Build CandidateProfile from the LLM-generated payload
            profile = CandidateProfile(
                user_id=str(candidate.user_id),
                name=personal.get("name") or "Unknown",
                location_preferences=prefs.get("locations", []),
                skills=personal.get("skills_detected", []),
                experience_level=recommended_roles[0].get("seniority", "entry") if recommended_roles else "entry",
                preferred_roles=preferred_roles,
                role_seniority_target=[r.get("seniority", "entry") for r in recommended_roles] or ["entry"],
                company_preferences={
                    "company_stage": [prefs.get("company_stage", "any")],
                    "company_size": [prefs.get("company_size", "1,10000")],
                    "industries": prefs.get("industry_interests", []),
                },
            )

            logger.info(f"[LeadSearch] Built CandidateProfile: roles={profile.preferred_roles}, locations={profile.location_preferences}")

            from services.lead_calibration.filter_generator_service import generate_apollo_filters
            filters = generate_apollo_filters(profile, db)

            logger.info(f"[LeadSearch] Filters generated — segments={len(filters.target_segments)}, "
                        f"locations={filters.person_locations}, "
                        f"industries={filters.organization_industries}, "
                        f"exclude_titles={len(filters.person_titles_exclude or [])}")
            for seg in filters.target_segments:
                logger.info(f"[LeadSearch]   Segment: size={seg.company_size_range}, titles={seg.person_titles[:5]}{'...' if len(seg.person_titles) > 5 else ''}")

        t_filter = time.perf_counter()
        logger.info(f"[LeadSearch] Filter generation: {(t_filter - t_start)*1000:.0f}ms")

        # Run blocking Apollo API calls in a thread to avoid blocking the event loop
        count = await asyncio.to_thread(
            collect_leads,
            filters=filters,
            candidate_id=candidate.id,
            target_leads=request.target_leads,
            db=db,
        )

        t_collect = time.perf_counter()
        logger.info(f"[LeadSearch] Lead collection complete: {count} leads in {(t_collect - t_start)*1000:.0f}ms")

        # Score all newly collected leads
        scored_count = 0
        try:
            scored_count = await asyncio.to_thread(_score_candidate_leads, db, candidate)
            logger.info(f"[LeadSearch] Leads scored: {scored_count}")
        except Exception as score_err:
            logger.error(f"[LeadSearch] Scoring failed (non-fatal): {score_err}", exc_info=True)

        t_end = time.perf_counter()
        logger.info(f"[LeadSearch] Pipeline complete: {(t_end - t_start)*1000:.0f}ms total, leads={count}, scored={scored_count}")

        return {"status": "success", "leads_collected": count, "leads_scored": scored_count}
    except Exception as e:
        logger.error(f"Discovery error for candidate {request.candidate_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# NOTE: GET /candidate/{id}/leads endpoint moved to routes_candidate.py
# to match the /api/v1/candidate/ URL prefix that the frontend expects.
