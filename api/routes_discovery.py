"""Discovery Routes — Lead discovery via Apollo."""

import asyncio
import logging
import time
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel

from database.session import get_db, SessionLocal
from database.models import User, Candidate, Lead, LeadScore
from services.lead_discovery.lead_collector_service import collect_leads, collect_dream_company_leads
from services.shared.schemas.filter_schema import LeadFilter
from services.shared.schemas.candidate_schema import CandidateProfile
from services.company_intelligence.company_enrichment_service import (
    bulk_enrich_top_companies,
    profile_to_llm_dict,
)
from services.lead_scoring.llm_justifier import justify_leads
from api.dependencies import get_current_user
from core.analytics import capture

# Top-K leads that get the full enrichment + LLM justification pass.
# 100 leads (~25 batches of 4 in parallel pool of 10 = 3 sequential rounds
# = ~30s extra). Worth the cost: every justified lead gets a tailored
# explanation instead of falling back to the generic FlashCard heuristic.
JUSTIFY_TOP_K = 100

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
            # Pipe niches through so the scorer can apply the niche-mismatch penalty.
            "niche_keywords": prefs.get("niche_keywords", []),
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
            "company_domain": lead.company_domain,
            "company_description": lead.company_description,
        }
        lead_dicts.append(d)
        lead_id_map[lead.id] = lead

    from services.lead_scoring.lead_scoring_service import score_and_select_leads as score_leads_svc
    scored = score_leads_svc(
        leads=lead_dicts,
        candidate_profile=candidate_profile,
        role_intelligence=role_intelligence,
        target_count=len(lead_dicts),
        dream_companies=candidate.dream_companies or [],
    )

    # Build LeadScore rows in memory first so we can attach justification_json
    # to the top-K before commit.
    score_rows: dict[int, LeadScore] = {}
    fallback_explanation = f"Scored against {', '.join(preferred_roles[:3])} (seniority={candidate_seniority})"
    count = 0
    for lead_dict in scored:
        lead_id = lead_dict.get("id")
        if not lead_id:
            continue
        ls = LeadScore(
            lead_id=lead_id,
            overall_score=lead_dict.get("score", 0),
            title_relevance=lead_dict.get("_title_score", 0),
            department_relevance=lead_dict.get("_dept_score", 0),
            industry_relevance=lead_dict.get("_industry_score", 0),
            seniority_relevance=lead_dict.get("_seniority_score", 0),
            location_relevance=lead_dict.get("_location_score", 0),
            explanation=fallback_explanation,
        )
        db.add(ls)
        score_rows[lead_id] = ls
        count += 1

    # ── Top-K LLM justification ───────────────────────────────────────────
    # Sort scored leads by overall score; enrich + justify the top JUSTIFY_TOP_K.
    # Tiebreaker: leads with a known company_domain rank first within the same
    # score bucket — they have richer signal and produce better justifications.
    top_leads = sorted(
        [ld for ld in scored if ld.get("id") in score_rows],
        key=lambda ld: (ld.get("score", 0), 1 if ld.get("company_domain") else 0),
        reverse=True,
    )[:JUSTIFY_TOP_K]

    if top_leads:
        try:
            # Apollo's people search returns near-zero org data on our plan
            # (no domain, no industry), so most leads have company_domain=None
            # but always have a company name. Pass both — the orchestrator
            # resolves missing domains via name-based Apollo lookup.
            top_companies = [
                {"domain": ld.get("company_domain"), "name": ld.get("company")}
                for ld in top_leads if ld.get("company") or ld.get("company_domain")
            ]
            unique_count = len({(c["domain"] or c["name"] or "").lower() for c in top_companies})
            logger.info("[JUSTIFY] enriching %d unique companies for top %d leads",
                        unique_count, len(top_leads))

            profiles = bulk_enrich_top_companies(db, top_companies, enable_scrape=True)
            company_payloads = {d: profile_to_llm_dict(p) for d, p in profiles.items()}

            # Backfill discovered domains onto Lead rows so subsequent runs
            # skip the name-resolution step.
            backfilled = 0
            lead_id_to_obj = {l.id: l for l in unscored}
            for ld in top_leads:
                lead_obj = lead_id_to_obj.get(ld.get("id"))
                if not lead_obj or lead_obj.company_domain:
                    continue
                profile = profiles.get(lead_obj.company)  # name-keyed lookup first
                if profile and profile.domain and profile.domain != (lead_obj.company or "").lower():
                    lead_obj.company_domain = profile.domain
                    backfilled += 1
            if backfilled:
                logger.info("[ENRICH] backfilled company_domain on %d leads", backfilled)

            # ── Domain-affinity adjustment (round-2) ─────────────────────────
            # Now that fact extraction has run during enrichment, compare each
            # company's `extracted_facts` to the candidate's resume_profile
            # subdomain + target_industries. Hard mismatches push leads below
            # the score floor so they're hidden from /candidate/{id}/leads.
            from services.lead_scoring.lead_scoring_service import apply_domain_affinity_to_top_leads
            resume_prof = candidate.resume_profile if isinstance(candidate.resume_profile, dict) else {}
            facts_by_domain = {d: (p.extracted_facts or {}) for d, p in profiles.items()}
            adjustments = apply_domain_affinity_to_top_leads(
                top_leads=top_leads,
                company_facts_by_domain=facts_by_domain,
                subdomain=resume_prof.get("subdomain"),
                target_industries=resume_prof.get("target_industries") or [],
                candidate_tech_stack=prefs.get("tech_stack") or [],
            )
            if adjustments:
                penalized = sum(1 for v in adjustments.values() if v < 0)
                boosted = sum(1 for v in adjustments.values() if v > 0)
                logger.info(
                    "[DOMAIN_AFFINITY] applied to %d/%d leads (penalized=%d, boosted=%d, subdomain=%s)",
                    len(adjustments), len(top_leads), penalized, boosted, resume_prof.get("subdomain"),
                )
                for lid, delta in adjustments.items():
                    row = score_rows.get(lid)
                    if row is not None and row.overall_score is not None:
                        row.overall_score = max(0, min(100, row.overall_score + delta))

            candidate_context = {
                "name": (parsed.get("personal_info") or {}).get("name"),
                "target_roles": candidate.target_roles or preferred_roles,
                "niche_keywords": prefs.get("niche_keywords") or [],
                "tech_stack": prefs.get("tech_stack") or [],
                "career_stage": prefs.get("career_stage"),
                "locations": prefs.get("locations") or [],
                "flex_notes": candidate.flex_notes or {},
                # Round-2: forward LLM-extracted resume_profile fields so the
                # justifier knows the candidate is specifically (e.g.) ASR/LLM,
                # not just generic ML.
                "subdomain": resume_prof.get("subdomain"),
                "top_skills": resume_prof.get("top_skills") or [],
                "target_industries": resume_prof.get("target_industries") or [],
            }

            justifications = justify_leads(
                leads_with_scores=top_leads,
                candidate_context=candidate_context,
                company_profiles=company_payloads,
            )

            for lid, payload in justifications.items():
                row = score_rows.get(lid)
                if row is not None:
                    row.justification_json = payload
            logger.info("[JUSTIFY] attached %d/%d justifications to top-K LeadScores",
                        len(justifications), len(top_leads))
        except Exception as e:
            logger.error("[JUSTIFY] top-K justification pipeline failed (non-fatal): %s", e, exc_info=True)

    db.commit()
    return count


def _score_candidate_leads_bg(candidate_id: int, user_id: str) -> None:
    """Background-task wrapper that opens its own DB session for scoring.

    Runs after the HTTP response has been sent so the client isn't blocked
    by the slow company-enrichment + LLM-justification pipeline.
    """
    db = SessionLocal()
    try:
        candidate = db.query(Candidate).filter_by(id=candidate_id).first()
        if not candidate:
            logger.warning("[SCORE_BG] candidate %d not found", candidate_id)
            return
        scored_count = _score_candidate_leads(db, candidate)
        logger.info("[SCORE_BG] candidate %d: scored %d leads", candidate_id, scored_count)

        from services.stage_tracking import safe_mark_stage
        safe_mark_stage(db, user_id, "leads_generated", candidate_id=candidate_id)

        from core.analytics import capture as _capture
        _capture("lead_scoring_completed", user_id, {
            "candidate_id": candidate_id,
            "leads_scored": scored_count,
        })
    except Exception as e:
        logger.error("[SCORE_BG] failed for candidate %d: %s", candidate_id, e, exc_info=True)
    finally:
        db.close()


@router.post("/search")
async def search_leads(
    request: DiscoveryRequest,
    background_tasks: BackgroundTasks,
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
    capture("lead_discovery_started", str(current_user.id), {
        "candidate_id": request.candidate_id,
        "target_leads": request.target_leads,
    })

    candidate = db.query(Candidate).filter_by(
        id=request.candidate_id, user_id=current_user.id
    ).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    # ── Idempotency guard ──────────────────────────────────────────────
    # If this candidate already has a substantial scored lead set from the
    # last 5 minutes, treat the call as a duplicate (e.g. browser retry,
    # double-click) and short-circuit. Stops wasted Apollo + LLM credits
    # when the frontend retries before the previous run finishes.
    from datetime import datetime, timedelta
    recent_lead_count = (
        db.query(Lead)
        .filter(Lead.candidate_id == candidate.id)
        .filter(Lead.created_at >= datetime.utcnow() - timedelta(minutes=5))
        .count()
    )
    if recent_lead_count >= 100:
        total_count = db.query(Lead).filter(Lead.candidate_id == candidate.id).count()
        scored_count = (
            db.query(LeadScore)
            .join(Lead, LeadScore.lead_id == Lead.id)
            .filter(Lead.candidate_id == candidate.id)
            .count()
        )
        logger.info(
            "[DISCOVERY] idempotency hit — candidate %s has %d leads in last 5min, "
            "returning early without re-running pipeline",
            candidate.id, recent_lead_count,
        )
        return {
            "status": "success",
            "leads_collected": total_count,
            "leads_scored": scored_count,
            "idempotent": True,
        }

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

            # Read explicit clarity from the persisted profile (Q2 of the quiz).
            # Map high/medium/low → int score for the existing CandidateProfile schema.
            _clarity_str = (prefs.get("clarity") or "medium").lower()
            clarity = {"high": 3, "medium": 1, "low": -1}.get(_clarity_str, 1)
            logger.info(f"[LeadSearch] Clarity from quiz: {_clarity_str} (score={clarity})")

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
                    "niche_keywords": prefs.get("niche_keywords", []),
                },
                work_preferences={
                    "work_mode": prefs.get("work_mode", "flexible"),
                },
                tech_stack=prefs.get("tech_stack", []),
                clarity_score=clarity,
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

        # Secondary pass: dream company leads
        dream_companies = candidate.dream_companies or []
        dream_count = 0
        if dream_companies:
            try:
                dream_count = await asyncio.to_thread(
                    collect_dream_company_leads,
                    base_filters=filters,
                    dream_companies=dream_companies,
                    candidate_id=candidate.id,
                    db=db,
                )
                count += dream_count
                logger.info(f"[LeadSearch] Dream company leads: {dream_count} additional")
            except Exception as dream_err:
                logger.error(f"[LeadSearch] Dream company search failed (non-fatal): {dream_err}", exc_info=True)

        t_end = time.perf_counter()
        duration = round(t_end - t_start, 1)
        logger.info(f"[LeadSearch] Collection complete in {duration*1000:.0f}ms — leads={count}; dispatching scoring to background")
        capture("lead_discovery_completed", str(current_user.id), {
            "candidate_id": request.candidate_id,
            "leads_found": count,
            "dream_company_leads": dream_count,
            "collection_duration_seconds": duration,
        })

        # Scoring (company enrichment + LLM justification) runs in the background
        # so the HTTP response returns before the 300s ingress timeout.
        if count > 0:
            background_tasks.add_task(
                _score_candidate_leads_bg, candidate.id, str(current_user.id)
            )

        return {"status": "success", "leads_collected": count, "leads_scored": 0, "scoring_async": True}
    except Exception as e:
        logger.error(f"Discovery error for candidate {request.candidate_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# NOTE: GET /candidate/{id}/leads endpoint moved to routes_candidate.py
# to match the /api/v1/candidate/ URL prefix that the frontend expects.
