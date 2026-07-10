"""Discovery Routes — Lead discovery via Apollo."""

import asyncio
import logging
import re
import time
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
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

# Top-K leads that get the full enrichment + LLM justification pass. This batch
# GATES the discovery loading screen (scoring-ready), so its size = how long the
# user waits at 96%. Justifying 300 took ~2+ min, stranding users at 96%. 100 cuts
# that ~3x; un-justified leads still show the client-side heuristic fallback
# (acceptable for the lower-ranked tail), and the background pass fills more later.
JUSTIFY_TOP_K = 100

# How many top-ranked unique companies get expensive LLM+Bing web research per
# discovery run (inline AND background). Each research call fans out into ~15-20
# Bing searches, so this is the single biggest driver of the "MS Bing Services"
# Azure line. Kept small deliberately: the top-N get rich company-specific facts,
# everyone else uses the 90k-company cache + a free logo-domain fallback below.
RESEARCH_TOP_N = 8

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/discovery", tags=["Discovery"])


def _launch_web_research_bg(top_companies: list, location_hint: Optional[list] = None) -> None:
    """Spawn a daemon thread to run LLM web research for uncached companies.

    Runs after justification is committed so it never blocks the scoring pipeline.
    Populates CompanyProfile cache so the next run for any of these companies
    gets company-specific bullets instead of generic ones.

    `location_hint` is forwarded to Apollo /mixed_companies/search inside
    bulk_enrich so ambiguous names resolve to the right org.
    """
    import threading
    from services.company_intelligence.company_enrichment_service import bulk_enrich_top_companies

    def _run():
        db = SessionLocal()
        try:
            bulk_enrich_top_companies(
                db, top_companies,
                enable_scrape=False, enable_llm_research=True,
                location_hint=location_hint,
            )
            db.commit()
        except Exception as exc:
            logger.warning("[WEB_RESEARCH_BG] failed: %s", exc)
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    logger.info("[WEB_RESEARCH_BG] launched for %d companies (location_hint=%s)",
                len(top_companies), location_hint)


def _fill_logo_domains_free(top_leads: list, lead_id_to_obj: dict) -> int:
    """Best-effort domain resolution for company LOGOS only — free, no Bing, no Apollo.

    Since we only deep-research RESEARCH_TOP_N companies, lower-ranked leads whose
    company isn't in the cache would have no domain → no logo. Clearbit's public
    autocomplete (name → domain, no auth, free) fills those so logos still render.
    Companies Clearbit doesn't know fall back to the client-side letter tile.
    """
    import requests
    from concurrent.futures import ThreadPoolExecutor

    # Unique company names among shown leads that still lack a domain.
    by_name: dict = {}
    for ld in top_leads:
        lo = lead_id_to_obj.get(ld.get("id"))
        if not lo or lo.company_domain or not (lo.company or "").strip():
            continue
        by_name.setdefault(lo.company.strip().lower(), (lo.company.strip(), []))[1].append(lo)
    if not by_name:
        return 0

    def _lookup(name: str):
        try:
            r = requests.get(
                "https://autocomplete.clearbit.com/v1/companies/suggest",
                params={"query": name}, timeout=4,
            )
            if r.ok:
                arr = r.json()
                if isinstance(arr, list) and arr:
                    return (arr[0] or {}).get("domain")
        except Exception:
            return None
        return None

    items = list(by_name.values())
    with ThreadPoolExecutor(max_workers=8) as pool:
        domains = list(pool.map(lambda it: _lookup(it[0]), items))

    filled = 0
    for (name, objs), dom in zip(items, domains):
        if not dom:
            continue
        for lo in objs:
            if not lo.company_domain:
                lo.company_domain = dom
                filled += 1
    return filled


class DiscoveryRequest(BaseModel):
    candidate_id: int
    target_leads: int = 800
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
            "company_stage": [prefs.get("company_stage", "any")],
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

    # ── Phase 1: Heuristic scoring across all leads ──────────────────────────
    # Fast, no LLM calls. Run on all leads, commit immediately.
    from services.lead_scoring.lead_scoring_service import score_and_select_leads as score_leads_svc
    scored = score_leads_svc(
        leads=lead_dicts,
        candidate_profile=candidate_profile,
        role_intelligence=role_intelligence,
        target_count=len(lead_dicts),
        dream_companies=candidate.dream_companies or [],
        company_fit_scores={},  # empty — company intel runs on top-K below
    )

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

    # Commit heuristic scores immediately — durable regardless of what LLM phases do.
    db.commit()
    logger.info("[SCORE_BG] Phase 1 done: %d heuristic scores committed", count)

    # ── Top-K LLM justification ───────────────────────────────────────────
    # Sort scored leads by overall score; enrich + justify the top JUSTIFY_TOP_K.
    # Tiebreaker: leads with a known company_domain rank first within the same
    # score bucket — they have richer signal and produce better justifications.
    top_leads = sorted(
        [ld for ld in scored if ld.get("id") in score_rows],
        key=lambda ld: (score_rows[ld["id"]].overall_score or 0, 1 if ld.get("company_domain") else 0),
        reverse=True,
    )[:JUSTIFY_TOP_K]

    if top_leads:
        try:
            # Apollo's people search returns near-zero org data on our plan
            # (no domain, no industry), so most leads have company_domain=None
            # but always have a company name. Pass both — the orchestrator
            # resolves missing/wrong domains via Apollo /mixed_companies/search
            # using `location_hint` to disambiguate common short names.
            top_companies = [
                {"domain": ld.get("company_domain"), "name": ld.get("company")}
                for ld in top_leads if ld.get("company") or ld.get("company_domain")
            ]
            unique_keys: list[str] = []
            unique_companies: list[dict] = []
            for c in top_companies:
                k = (c.get("domain") or c.get("name") or "").lower()
                if k and k not in unique_keys:
                    unique_keys.append(k)
                    unique_companies.append(c)

            # Pull the candidate's location preferences as a hint for the
            # Apollo company-search disambiguation. Without this, ambiguous
            # names like "Comet" or "Swish" resolve to the most-indexed
            # global brand instead of the actual Indian / regional company
            # the contact works at.
            location_hint = prefs.get("locations") or []

            logger.info("[JUSTIFY] enriching %d unique companies for top %d leads (location_hint=%s)",
                        len(unique_companies), len(top_leads), location_hint)

            # Warm cache for the top-N unique companies via inline LLM web research.
            # First-time candidates have no cached company profiles → justifier
            # gets empty facts → writes generic bullets → banned phrases → null.
            # Researching the top-N inline ensures the justifier has real data.
            # Companies after the top-N fall back to cache-only (fine — they're
            # lower ranked; they still get logos via the free domain fallback, and
            # the cache fills over time). RESEARCH_TOP_N bounds the Bing spend.
            top_research = unique_companies[:RESEARCH_TOP_N]
            if top_research:
                logger.info("[JUSTIFY] inline LLM research for top-%d companies", len(top_research))
                bulk_enrich_top_companies(
                    db, top_research, enable_scrape=False, enable_llm_research=True,
                    location_hint=location_hint,
                )

            # Cache-only pass for all companies — the top-N are now cached.
            profiles = bulk_enrich_top_companies(
                db, top_companies, enable_scrape=False, enable_llm_research=False
            )
            company_payloads = {d: profile_to_llm_dict(p) for d, p in profiles.items()}

            # Backfill / correct discovered domains onto Lead rows so subsequent
            # runs use the right domain (and we self-heal previously-wrong ones
            # written before the Apollo company-search disambiguation existed).
            backfilled = 0
            corrected = 0
            lead_id_to_obj = {l.id: l for l in unscored}
            for ld in top_leads:
                lead_obj = lead_id_to_obj.get(ld.get("id"))
                if not lead_obj:
                    continue
                profile = profiles.get(lead_obj.company)  # name-keyed lookup
                if not (profile and profile.domain):
                    continue
                if profile.domain == (lead_obj.company or "").lower():
                    continue  # company-name fallback / negative-cache sentinel — not a real domain
                if not lead_obj.company_domain:
                    lead_obj.company_domain = profile.domain
                    backfilled += 1
                elif lead_obj.company_domain != profile.domain:
                    logger.info("[ENRICH] corrected lead %d domain %s → %s",
                                lead_obj.id, lead_obj.company_domain, profile.domain)
                    lead_obj.company_domain = profile.domain
                    corrected += 1
            if backfilled or corrected:
                logger.info("[ENRICH] domain writeback: backfilled %d, corrected %d leads",
                            backfilled, corrected)

            # Ensure logos render for the shown leads we did NOT deep-research:
            # free Clearbit name→domain lookup (no Bing, no Apollo). Best-effort.
            try:
                logo_filled = _fill_logo_domains_free(top_leads, lead_id_to_obj)
                if logo_filled:
                    logger.info("[ENRICH] logo domains filled via free lookup for %d leads", logo_filled)
            except Exception as e:
                logger.warning("[ENRICH] free logo-domain fill skipped: %s", e)

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
                "subdomain": resume_prof.get("subdomain"),
                "top_skills": resume_prof.get("top_skills") or [],
                "target_industries": resume_prof.get("target_industries") or [],
            }

            # Commit score adjustments (affinity + domain backfills) NOW, before
            # the LLM justification calls. Holding the session open for 1-2 minutes
            # causes Postgres to drop the idle connection and silently discard all work.
            score_id_map = {lid: row.id for lid, row in score_rows.items()}
            try:
                db.commit()
                logger.info("[SCORE_BG] Pre-justification commit done (affinity + backfills)")
            except Exception as pre_commit_err:
                logger.warning("[SCORE_BG] Pre-justification commit failed: %s", pre_commit_err)
                try:
                    db.rollback()
                except Exception:
                    pass
            db.close()

            justifications = justify_leads(
                leads_with_scores=top_leads,
                candidate_context=candidate_context,
                company_profiles=company_payloads,
            )

            # Save in a fresh short-lived session — original session was closed above.
            from database.session import SessionLocal as _FreshSession
            db_save = _FreshSession()
            try:
                saved = 0
                for lid, payload in justifications.items():
                    score_id = score_id_map.get(lid)
                    if score_id:
                        ls_row = db_save.get(LeadScore, score_id)
                        if ls_row:
                            ls_row.justification_json = payload
                            saved += 1
                db_save.commit()
                logger.info("[JUSTIFY] saved %d/%d justifications to DB", saved, len(justifications))
            except Exception as save_err:
                logger.error("[JUSTIFY] save failed: %s", save_err)
                try:
                    db_save.rollback()
                except Exception:
                    pass
            finally:
                db_save.close()

            # Kick off LLM web research for uncached companies in a daemon thread.
            # Same RESEARCH_TOP_N ceiling as the inline pass — the top-N are already
            # researched inline above and now cached, so this background pass adds
            # ~0 new Bing calls. It exists only as a safety net if the inline pass
            # was skipped/failed. This is the second cap that bounds Bing spend.
            _launch_web_research_bg(unique_companies[:RESEARCH_TOP_N], location_hint=location_hint)

        except Exception as e:
            logger.error("[JUSTIFY] top-K justification pipeline failed (non-fatal): %s", e, exc_info=True)
            try:
                db.rollback()
            except Exception:
                pass

    return count


def _score_candidate_leads_sync(candidate_id: int, user_id: str) -> int:
    """Opens its own DB session, runs Phase 1 (heuristic) + Phase 3 (justification).

    Called via asyncio.to_thread from the search endpoint so scoring completes
    before the HTTP response is returned — leads arrive with bullets already attached.
    Phase 2 (company intel) runs separately in background afterward.
    """
    scored_count = 0
    db = SessionLocal()
    try:
        candidate = db.query(Candidate).filter_by(id=candidate_id).first()
        if not candidate:
            logger.warning("[SCORE_SYNC] candidate %d not found", candidate_id)
            return 0
        scored_count = _score_candidate_leads(db, candidate)
        logger.info("[SCORE_SYNC] candidate %d: scored+justified %d leads", candidate_id, scored_count)
    except Exception as e:
        logger.error("[SCORE_SYNC] failed for candidate %d: %s", candidate_id, e, exc_info=True)
        return 0
    finally:
        db.close()  # safe even if _score_candidate_leads already closed it

    # Post-scoring housekeeping in a fresh session (_score_candidate_leads closed the original).
    db2 = SessionLocal()
    try:
        from services.stage_tracking import safe_mark_stage
        safe_mark_stage(db2, user_id, "leads_generated", candidate_id=candidate_id)
        from core.analytics import capture as _capture
        _capture("lead_scoring_completed", user_id, {
            "candidate_id": candidate_id,
            "leads_scored": scored_count,
        })
    except Exception as e:
        logger.warning("[SCORE_SYNC] post-scoring housekeeping failed: %s", e)
    finally:
        db2.close()
    return scored_count


def _run_company_intel_bg(candidate_id: int) -> None:
    """Background: Phase 2 company intelligence score adjustments.

    Runs after leads are already shown to the user. Fetches existing heuristic
    scores, applies LLM company-fit adjustments, and commits updated scores.
    """
    db = SessionLocal()
    try:
        candidate = db.query(Candidate).filter_by(id=candidate_id).first()
        if not candidate:
            return

        parsed = candidate.parsed_json or {}
        career = parsed.get("career_analysis", {})
        prefs = parsed.get("preferences", {})
        preferred_roles = [r.get("title", "") for r in career.get("recommended_roles", []) if r.get("title")]
        if not preferred_roles:
            preferred_roles = candidate.target_roles or ["Software Engineer"]

        leads = db.query(Lead).filter_by(candidate_id=candidate_id).all()
        if not leads:
            return

        lead_dicts = [{
            "id": l.id,
            "company": l.company,
            "company_domain": l.company_domain,
            "industry": l.industry,
            "company_size": l.company_size,
            "company_description": l.company_description,
        } for l in leads]

        score_rows = {
            s.lead_id: s
            for s in db.query(LeadScore).filter(
                LeadScore.lead_id.in_([l.id for l in leads])
            ).all()
        }

        from services.lead_scoring.company_intelligence_service import evaluate_company_fit
        candidate_prefs_for_intel = {
            "company_stage": [prefs.get("company_stage", "any")],
            "niche_keywords": prefs.get("niche_keywords", []),
            "preferred_roles": preferred_roles,
            "archetype_label": (candidate.resume_profile or {}).get("archetype_label", ""),
            "company_type_avoid": (candidate.resume_profile or {}).get("company_type_avoid", []),
        }
        company_fit_scores = evaluate_company_fit(lead_dicts, candidate_prefs_for_intel, db)
        logger.info("[COMPANY_INTEL_BG] candidate %d: %d companies evaluated", candidate_id, len(company_fit_scores))

        for ld in lead_dicts:
            name_lower = (ld.get("company") or "").lower()
            fit = company_fit_scores.get(name_lower)
            if fit is None:
                continue
            row = score_rows.get(ld["id"])
            if row is None:
                continue
            fit_pts = round((fit - 1) / 9 * 15)
            current = row.overall_score or 0
            row.overall_score = max(0, min(100, current + fit_pts - 7))

        db.commit()
        logger.info("[COMPANY_INTEL_BG] candidate %d: score adjustments committed", candidate_id)
    except Exception as e:
        logger.warning("[COMPANY_INTEL_BG] failed for candidate %d: %s", candidate_id, e)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _score_candidate_leads_bg(candidate_id: int, user_id: str) -> None:
    """Fallback: runs all phases sequentially (used for manual rescoring)."""
    db = SessionLocal()
    try:
        candidate = db.query(Candidate).filter_by(id=candidate_id).first()
        if not candidate:
            return
        _score_candidate_leads(db, candidate)
    except Exception as e:
        logger.error("[SCORE_BG] failed for candidate %d: %s", candidate_id, e, exc_info=True)
    finally:
        db.close()
    _run_company_intel_bg(candidate_id)


@router.post("/search")
async def search_leads(
    request: DiscoveryRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Execute lead discovery based on candidate profile."""
    t_start = time.perf_counter()
    # Hard minimum: always retrieve at least 800 leads so after LLM filtering
    # the user still sees ~500 high+medium signal leads.
    if request.target_leads < 800:
        logger.info(f"[DISCOVERY] target_leads={request.target_leads} below minimum, enforcing 800")
        request.target_leads = 800
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

    probe_exclusions: list[str] = []  # populated inside else block; empty for pre-built filters path

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

            # Career Strategist — LLM generates title clusters + Apollo strategy
            from services.candidate_intelligence.career_strategist import run_career_strategist
            t_pre_strategist = time.perf_counter()
            search_strategy = await asyncio.to_thread(
                run_career_strategist,
                candidate.resume_profile or {},
                prefs,
                preferred_roles,
                candidate.flex_notes,
            )
            logger.info(
                f"[LeadSearch] Career Strategist: {'strategy generated' if search_strategy else 'fallback to rules'} "
                f"in {(time.perf_counter() - t_pre_strategist)*1000:.0f}ms"
            )

            from services.lead_calibration.filter_generator_service import generate_apollo_filters
            filters = generate_apollo_filters(profile, db, search_strategy=search_strategy)

            logger.info(f"[LeadSearch] Filters generated (path={'llm' if search_strategy else 'rules'}) — "
                        f"segments={len(filters.target_segments)}, "
                        f"locations={filters.person_locations}, "
                        f"exclude_titles={len(filters.person_titles_exclude or [])}")
            for seg in filters.target_segments:
                logger.info(f"[LeadSearch]   Segment: size={seg.company_size_range}, titles={seg.person_titles[:5]}{'...' if len(seg.person_titles) > 5 else ''}")

            # LLM quality probe — lives inside the else block so prefs/preferred_roles
            # are always defined. Pre-built filters (request.filters path) skip the probe.
            from services.lead_discovery.lead_collector_service import quality_probe_loop
            candidate_prefs_for_probe = {
                "company_stage": [prefs.get("company_stage", "any")],
                "niche_keywords": prefs.get("niche_keywords", []),
                "preferred_roles": preferred_roles,
                "archetype_label": (candidate.resume_profile or {}).get("archetype_label", ""),
                "company_type_avoid": (candidate.resume_profile or {}).get("company_type_avoid", []),
            }
            t_pre_probe = time.perf_counter()
            filters, probe_exclusions = await asyncio.to_thread(
                quality_probe_loop,
                filters,
                candidate_prefs_for_probe,
                1,  # max_iterations — 1 probe is enough; 3 added ~26s of sync latency
            )
            logger.info(
                f"[LeadSearch] Quality probe complete: {(time.perf_counter() - t_pre_probe)*1000:.0f}ms "
                f"exclusions={probe_exclusions}"
            )

        t_filter = time.perf_counter()
        logger.info(f"[LeadSearch] Filter generation + probe: {(t_filter - t_start)*1000:.0f}ms")

        # Run blocking Apollo API calls in a thread to avoid blocking the event loop
        count = await asyncio.to_thread(
            collect_leads,
            filters=filters,
            candidate_id=candidate.id,
            target_leads=request.target_leads,
            db=db,
            excluded_companies=probe_exclusions or None,
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

        # Scoring runs in background — synchronous scoring times out (4+ min
        # exceeds ingress/browser limits). Frontend loading screen polls
        # /discovery/scoring-ready/{candidate_id} until bullets are ready.
        if count > 0:
            background_tasks.add_task(
                _score_candidate_leads_sync, candidate.id, str(current_user.id)
            )
            background_tasks.add_task(_run_company_intel_bg, candidate.id)

        return {"status": "success", "leads_collected": count, "leads_scored": 0, "scoring_async": True}
    except Exception as e:
        logger.error(f"Discovery error for candidate {request.candidate_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/scoring-ready/{candidate_id}")
async def scoring_ready(
    candidate_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Poll this until bullets are ready. Frontend keeps loading screen up until then."""
    candidate = db.query(Candidate).filter_by(
        id=candidate_id, user_id=current_user.id
    ).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    total = db.query(Lead).filter_by(candidate_id=candidate_id).count()
    scored = (
        db.query(LeadScore)
        .join(Lead, LeadScore.lead_id == Lead.id)
        .filter(Lead.candidate_id == candidate_id)
        .count()
    )
    with_bullets = (
        db.query(LeadScore)
        .join(Lead, LeadScore.lead_id == Lead.id)
        .filter(Lead.candidate_id == candidate_id, LeadScore.justification_json.isnot(None))
        .count()
    )
    # Unblock the loading screen as soon as (fast, heuristic) scoring is done — we no
    # longer wait for the slow LLM justification pass here. The results page polls and
    # renders justifications as they stream in, so the user gets onto their leads in
    # ~seconds instead of staring at 96% for minutes. with_bullets stays in the response
    # as a progress signal for the results page.
    ready = scored >= max(1, total * 0.9)
    return {
        "ready": ready,
        "total": total,
        "scored": scored,
        "with_bullets": with_bullets,
    }


class ImportFromOutreachRequest(BaseModel):
    candidate_id: int                    # target (LinkedIn) candidate to import INTO
    source_candidate_id: Optional[int] = None  # specific source; else newest other candidate with leads


class LinkedInDiscoverRequest(BaseModel):
    candidate_id: int
    target_role: Optional[str] = None        # override; else derived from candidate profile
    locations: Optional[List[str]] = None    # override; else derived from candidate profile
    limit: int = 300                         # web search is cheap; default to a full set


def _derive_role_and_locations(candidate: Candidate) -> tuple[str, list[str]]:
    """Target role + locations for lead discovery.

    HIGHEST priority is what the user actually typed in the LinkedIn onboarding
    form (stored in flex_notes by save_flex_notes). Only if that's absent do we
    fall back to résumé-derived targeting — otherwise a user searching for
    'Marketing Associate in Paris' wrongly gets their résumé's role/country."""
    parsed = candidate.parsed_json or {}
    prefs = parsed.get("preferences", {}) if isinstance(parsed, dict) else {}
    career = parsed.get("career_analysis", {}) if isinstance(parsed, dict) else {}
    profile = candidate.resume_profile if isinstance(candidate.resume_profile, dict) else {}
    flex = candidate.flex_notes if isinstance(candidate.flex_notes, dict) else {}

    # 1. User-typed input wins outright.
    role_input = (flex.get("target_role_user_input") or "").strip()
    loc_input = (flex.get("location_user_input") or "").strip()
    if role_input:
        # Split a "Paris, France" style location into parts; "remote" → no geo filter.
        if loc_input and loc_input.lower() not in ("remote", "anywhere"):
            locs = [p.strip() for p in re.split(r"[,/]| or ", loc_input) if p.strip()]
        else:
            locs = []
        return role_input, locs

    # 2. Fall back to résumé-derived role.
    role = ""
    target_roles = candidate.target_roles if isinstance(candidate.target_roles, list) else []
    if target_roles:
        first = target_roles[0]
        role = first if isinstance(first, str) else (first.get("role") or first.get("title") or "")
    if not role:
        rec = career.get("recommended_roles") or []
        if rec and isinstance(rec[0], dict):
            role = rec[0].get("title") or ""
    if not role:
        likely = profile.get("likely_roles") if isinstance(profile.get("likely_roles"), list) else []
        if likely:
            role = str(likely[0])
    role = (role or "Marketing Manager").strip()

    locations: list[str] = []
    if loc_input and loc_input.lower() not in ("remote", "anywhere"):
        locations = [p.strip() for p in re.split(r"[,/]| or ", loc_input) if p.strip()]
    for loc in (prefs.get("locations") or []):
        if isinstance(loc, str) and loc.strip() and loc.strip() not in locations:
            locations.append(loc.strip())
    geo = profile.get("geography") if isinstance(profile.get("geography"), dict) else {}
    for key in ("city", "country"):
        v = (geo.get(key) or "").strip()
        if v and v not in locations:
            locations.append(v)
    if not locations:
        locations = ["India"]
    return role, locations


async def _run_linkedin_discover(candidate_id: int, user_id: str,
                                 role_override: Optional[str], loc_override: Optional[list],
                                 limit: int = 30):
    """Background worker: DDG x-ray discovery → Lead rows with linkedin_url.
    Never holds a DB connection across the web work, and for high-volume runs
    persists each batch as it arrives (so a 5-10 min run isn't lost if the pod
    restarts mid-sweep)."""
    from services.linkedin_outreach.web_discovery import discover_leads_via_search

    db = SessionLocal()
    try:
        candidate = db.query(Candidate).filter_by(id=candidate_id, user_id=user_id).first()
        if not candidate:
            return
        role, locations = _derive_role_and_locations(candidate)
    finally:
        db.close()
    if role_override:
        role = role_override.strip()
    if loc_override:
        locations = [l for l in loc_override if l and l.strip()] or locations

    # Clear prior web-discovered leads up front so a re-search with new criteria
    # doesn't mix stale results in. Only DDG-sourced rows (apollo_id NULL,
    # linkedin_url set) are removed; imported Apollo leads are left untouched.
    db = SessionLocal()
    try:
        db.query(Lead).filter(
            Lead.candidate_id == candidate_id,
            Lead.apollo_id.is_(None),
            Lead.linkedin_url.isnot(None),
        ).delete(synchronize_session=False)
        db.commit()
        seen_urls = {
            u for (u,) in db.query(Lead.linkedin_url).filter_by(candidate_id=candidate_id).all() if u
        }
    finally:
        db.close()

    total = 0

    async def _persist(batch: list[dict]):
        nonlocal total
        bdb = SessionLocal()
        try:
            for p in batch:
                url = (p.get("profile_url") or "").strip()
                if not url or url in seen_urls:
                    continue
                bdb.add(Lead(
                    candidate_id=candidate_id,
                    name=p.get("name") or "",
                    title=p.get("headline") or role,
                    company=p.get("company"),
                    linkedin_url=url,
                    status="new",
                ))
                seen_urls.add(url)
                total += 1
            bdb.commit()
        finally:
            bdb.close()

    await discover_leads_via_search(
        target_role=role, locations=locations, limit=limit, on_batch=_persist,
    )
    logger.info(
        "[LinkedInDiscover] candidate=%s role=%r locations=%r limit=%d created=%d",
        candidate_id, role, locations, limit, total,
    )


@router.post("/linkedin-discover")
async def linkedin_discover(
    body: LinkedInDiscoverRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Find real LinkedIn profiles (name + profile URL) for a candidate via
    public web search (DDG x-ray through the residential proxy). No Apollo
    credits, no LinkedIn login, no extension. Runs in the background (1-2 min);
    the frontend polls GET /candidate/{id}/leads for the new rows.
    """
    candidate = db.query(Candidate).filter_by(
        id=body.candidate_id, user_id=current_user.id
    ).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    role, locations = _derive_role_and_locations(candidate)
    if body.target_role:
        role = body.target_role.strip()
    if body.locations:
        locations = [l for l in body.locations if l and l.strip()] or locations

    background_tasks.add_task(
        _run_linkedin_discover,
        candidate_id=body.candidate_id,
        user_id=str(current_user.id),
        role_override=body.target_role,
        loc_override=body.locations,
        limit=max(5, min(body.limit, 400)),
    )
    return {"status": "started", "role": role, "locations": locations, "target": body.limit}


@router.get("/outreach-sources/{candidate_id}")
async def outreach_sources(
    candidate_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List this user's OTHER candidates that have discovered leads, so the
    LinkedIn flow can offer 'Export from Outreach'."""
    target = db.query(Candidate).filter_by(id=candidate_id, user_id=current_user.id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Candidate not found")
    rows = (
        db.query(Candidate.id, Candidate.created_at, func.count(Lead.id).label("lead_count"))
        .join(Lead, Lead.candidate_id == Candidate.id)
        .filter(Candidate.user_id == current_user.id, Candidate.id != candidate_id)
        .group_by(Candidate.id, Candidate.created_at)
        .order_by(Candidate.created_at.desc())
        .all()
    )
    return {
        "sources": [
            {"candidate_id": r.id, "lead_count": r.lead_count,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows if r.lead_count > 0
        ]
    }


@router.post("/import-from-outreach")
async def import_from_outreach(
    body: ImportFromOutreachRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Copy already-discovered leads (and their scores) from one of the user's
    outreach candidates into the target LinkedIn candidate. Dedupes by apollo_id /
    linkedin_url. Lets users reuse Outreach leads in the LinkedIn flow without
    re-running discovery."""
    target = db.query(Candidate).filter_by(id=body.candidate_id, user_id=current_user.id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Target candidate not found")

    # Pick the source candidate: explicit, else newest OTHER candidate with leads.
    src_q = (
        db.query(Candidate.id)
        .join(Lead, Lead.candidate_id == Candidate.id)
        .filter(Candidate.user_id == current_user.id, Candidate.id != body.candidate_id)
    )
    if body.source_candidate_id:
        src_q = src_q.filter(Candidate.id == body.source_candidate_id)
    src_q = src_q.group_by(Candidate.id, Candidate.created_at).order_by(Candidate.created_at.desc())
    src_row = src_q.first()
    if not src_row:
        raise HTTPException(status_code=404, detail="No outreach leads found to import.")
    source_id = src_row.id

    # Existing dedupe keys on the target
    existing = db.query(Lead.apollo_id, Lead.linkedin_url).filter_by(candidate_id=body.candidate_id).all()
    seen_apollo = {a for a, _ in existing if a}
    seen_li = {u for _, u in existing if u}

    src_leads = db.query(Lead).filter_by(candidate_id=source_id).all()
    imported = 0
    for sl in src_leads:
        if sl.apollo_id and sl.apollo_id in seen_apollo:
            continue
        if sl.linkedin_url and sl.linkedin_url in seen_li:
            continue
        new_lead = Lead(
            candidate_id=body.candidate_id,
            apollo_id=sl.apollo_id, name=sl.name, title=sl.title, company=sl.company,
            industry=sl.industry, location=sl.location, linkedin_url=sl.linkedin_url,
            email=sl.email, company_size=sl.company_size, email_verified=sl.email_verified,
            company_description=sl.company_description, company_domain=sl.company_domain,
            status=sl.status or "new",
        )
        db.add(new_lead)
        db.flush()  # get new_lead.id
        # copy the best score row if present
        src_score = db.query(LeadScore).filter_by(lead_id=sl.id).first()
        if src_score:
            db.add(LeadScore(
                lead_id=new_lead.id,
                overall_score=src_score.overall_score, title_relevance=src_score.title_relevance,
                department_relevance=src_score.department_relevance, industry_relevance=src_score.industry_relevance,
                seniority_relevance=src_score.seniority_relevance, location_relevance=src_score.location_relevance,
                explanation=src_score.explanation, justification_json=src_score.justification_json,
            ))
        if sl.apollo_id: seen_apollo.add(sl.apollo_id)
        if sl.linkedin_url: seen_li.add(sl.linkedin_url)
        imported += 1
    db.commit()
    return {"imported": imported, "source_candidate_id": source_id, "total_now":
            db.query(Lead).filter_by(candidate_id=body.candidate_id).count()}


# NOTE: GET /candidate/{id}/leads endpoint moved to routes_candidate.py
# to match the /api/v1/candidate/ URL prefix that the frontend expects.
