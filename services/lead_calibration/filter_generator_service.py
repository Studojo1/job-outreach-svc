"""Service for generating segmented lead search filters (production grade).

Includes Apollo-normalized industry names and location formats.
"""

from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy.orm import Session

from services.shared.schemas.candidate_schema import CandidateProfile
from services.shared.schemas.filter_schema import LeadFilter
from services.shared.schemas.target_segment_schema import TargetSegment
from services.shared.decision_maker_engine import generate_titles_by_company_size
from services.shared.apollo_normalizer import normalize_industries, normalize_locations
from core.logger import get_logger

logger = get_logger(__name__)

# Per Phase A audit (May 2026): organization_industries dropped from default —
# kept off unless caller explicitly passes industries. Niche keywords
# (q_organization_keyword_tags) are the preferred vertical filter.
APPLY_INDUSTRY_FILTER_BY_DEFAULT = False

# Default job-posting recency window. Phase A confirmed 30d ≈ 60d ≈ 90d in
# volume but 30d gives sharpest signal.
JOB_POSTED_RECENCY_DAYS = 30


def _today_iso() -> str:
    return datetime.utcnow().date().isoformat()


def _days_ago_iso(days: int) -> str:
    return (datetime.utcnow().date() - timedelta(days=days)).isoformat()

# Used only when generate_titles_by_company_size returns nothing AND the role
# couldn't be classified. Kept generic-management; function-specific fallbacks
# live in lead_collector_service._FUNCTION_FALLBACK_TITLES.
FALLBACK_TITLES = [
    "Operations Manager", "Head of Operations", "Director of Operations",
    "Chief of Staff", "General Manager", "Business Operations Manager",
]

# Default exclusion titles — roles we should never target as hiring managers.
# Apollo's `person_not_titles` does substring matching, so these block any
# title containing these words. Expanded May 4 2026 after manual lead review
# showed Operations Managers, Sales Managers, Customer Success Managers,
# and Junior ICs were leaking through and crowding out actual hiring managers.
DEFAULT_EXCLUSION_TITLES = [
    # Recruitment / HR (was already there)
    "Recruiter",
    "Talent Acquisition",
    "People Operations",
    # Sub-grad ranks — never hiring managers
    "Intern",
    "Student",
    "Apprentice",
    "Trainee",
    # Cross-functional managers that flood the data/eng title list with noise
    "Operations Manager",
    "Sales Manager",
    "Account Manager",
    "Customer Success Manager",
    "Customer Success",
    "Account Executive",
    "Business Analyst",
    "Strategy Manager",
    "Project Manager",
    "Project Coordinator",
    "Office Manager",
    "Administrative Assistant",
    "Executive Assistant",
    "Inside Sales",
    "Field Sales",
    # HR Manager — kept as a keyword block (HR Business Partner etc. still excluded
    # via the People Operations entry)
    "HR Manager",
]


def _seniorities_for_experience_level(exp_level: str) -> List[str]:
    """Map our internal experience level to Apollo's `person_seniorities` codes.

    Apollo seniority codes (per their /api/v1/mixed_people/api_search docs):
      owner, founder, c_suite, partner, vp, head, director, manager, senior, entry, intern.

    We never want intern/entry as hiring managers. Beyond that, we widen the
    floor as the candidate gets more senior — a senior IC will reach out to
    VPs/founders, an entry-level student should reach out to managers/directors
    (peers and one-up).
    """
    if exp_level == "senior":
        return ["owner", "founder", "c_suite", "partner", "vp", "head", "director", "manager"]
    if exp_level == "mid":
        return ["vp", "head", "director", "manager"]
    # entry / student / graduate
    return ["head", "director", "manager"]


def generate_apollo_filters(candidate_profile: CandidateProfile, db: Session) -> LeadFilter:
    """Convert a candidate profile into production-grade segmented Apollo filters.

    Uses Apollo-normalized industry names and location formats.
    """
    logger.info(
        "Generating production filters for user_id=%s name=%s",
        candidate_profile.user_id,
        candidate_profile.name,
    )

    # ── Step 1: Detect candidate roles ───────────────────────────────────
    roles = candidate_profile.preferred_roles
    logger.info("Candidate preferred_roles: %s", roles)

    # ── Step 2: Generate segments via Decision Maker Engine ──────────────
    # Map experience level from candidate profile to the title generation system
    exp_level_map = {
        "student": "entry",
        "grad": "entry",
        "graduate": "entry",
        "entry": "entry",
        "switching": "mid",
        "career_switching": "mid",
        "experienced": "senior",
    }
    exp_level = exp_level_map.get((candidate_profile.experience_level or "entry").lower(), "entry")
    logger.info("Candidate experience level: %s → targeting %s seniority titles", candidate_profile.experience_level, exp_level)

    all_segments: List[TargetSegment] = []
    seen_key = set()
    for role in roles:
        role_segments = generate_titles_by_company_size(role, experience_level=exp_level)
        for seg in role_segments:
            merged_titles = []
            for t in seg.person_titles:
                key = (seg.company_size_range, t)
                if key not in seen_key:
                    seen_key.add(key)
                    merged_titles.append(t)
            existing = next(
                (s for s in all_segments if s.company_size_range == seg.company_size_range),
                None,
            )
            if existing:
                existing.person_titles.extend(merged_titles)
            elif merged_titles:
                all_segments.append(
                    TargetSegment(
                        company_size_range=seg.company_size_range,
                        person_titles=merged_titles,
                    )
                )

    if not all_segments:
        all_segments = [
            TargetSegment(company_size_range="1,10000", person_titles=list(FALLBACK_TITLES))
        ]
        logger.info("No segments generated, using fallback")

    logger.info("Total segments: %d", len(all_segments))
    for seg in all_segments:
        logger.info("  Segment '%s': %d titles — %s", seg.company_size_range, len(seg.person_titles), seg.person_titles[:5])

    # ── Step 3: Exclusion titles ─────────────────────────────────────────
    person_titles_exclude = list(DEFAULT_EXCLUSION_TITLES)
    logger.info("Exclusion titles: %s", person_titles_exclude)

    # ── Step 4: Location (NORMALIZED) ────────────────────────────────────
    raw_locations = candidate_profile.location_preferences or []
    normalized_locs = normalize_locations(raw_locations)
    logger.info("[LeadSearch] Location filter: raw=%s, normalized=%s", raw_locations, normalized_locs)

    # ── Step 5: Industries — DROPPED as default per Phase A audit ────────
    # Niche keywords (q_organization_keyword_tags) deliver the same intent
    # with better quality lift on Indian companies. Kept opt-in if caller
    # explicitly enables APPLY_INDUSTRY_FILTER_BY_DEFAULT.
    company_prefs = candidate_profile.company_preferences or {}
    normalized_industries = None
    if APPLY_INDUSTRY_FILTER_BY_DEFAULT:
        raw_industries = company_prefs.get("industries") or []
        normalized_industries = normalize_industries(raw_industries) or None
        logger.info("[LeadSearch] Industry filter: raw=%s, normalized=%s", raw_industries, normalized_industries)
    else:
        logger.info("[LeadSearch] Industry filter: skipped (per Phase A audit — niche keywords used instead)")

    # ── Step 6: New Phase A audit-passed filters ─────────────────────────
    clarity = candidate_profile.clarity_score or 0
    work_prefs = candidate_profile.work_preferences or {}
    work_mode = (work_prefs.get("work_mode") or "").lower()

    # 6a. Currently-hiring-for: feed candidate's preferred roles as posting titles
    q_org_job_titles = list(roles)[:8] if roles else None
    if q_org_job_titles:
        logger.info("[LeadSearch] q_organization_job_titles: %s", q_org_job_titles)

    # 6b. Posting recency — pair with currently-hiring
    org_job_posted_at_range = None
    if q_org_job_titles:
        org_job_posted_at_range = {
            "min": _days_ago_iso(JOB_POSTED_RECENCY_DAYS),
            "max": _today_iso(),
        }
        logger.info("[LeadSearch] organization_job_posted_at_range: %s", org_job_posted_at_range)

    # 6c. Niche keywords — gated on clarity ≥ 0 (medium + high). OR'd up to 3.
    niche_keywords = None
    if clarity >= 0:
        raw_niches = company_prefs.get("niche_keywords") or []
        if raw_niches:
            # Normalize: lowercase, strip whitespace, take first 3
            niche_keywords = [k.strip().lower() for k in raw_niches if k and k.strip()][:3]
            if not niche_keywords:
                niche_keywords = None
        if niche_keywords:
            logger.info("[LeadSearch] q_organization_keyword_tags (OR): %s", niche_keywords)

    # 6d. Tech stack — gated on clarity ≥ 3 (high) AND populated
    tech_stack = None
    if clarity >= 3 and candidate_profile.tech_stack:
        tech_stack = [t.strip().lower().replace(" ", "_") for t in candidate_profile.tech_stack if t.strip()][:3]
        if not tech_stack:
            tech_stack = None
        if tech_stack:
            logger.info("[LeadSearch] currently_using_any_of_technology_uids: %s", tech_stack)

    # 6e. Job-posting location — only for in-office / hybrid candidates
    org_job_locations = None
    if work_mode in ("onsite", "hybrid") and normalized_locs:
        org_job_locations = normalized_locs
        logger.info("[LeadSearch] organization_job_locations (work_mode=%s): %s", work_mode, org_job_locations)

    # 6f. Past titles — free win, derived from preferred_roles
    person_past_titles = list(roles)[:5] if roles else None
    if person_past_titles:
        logger.info("[LeadSearch] person_past_titles (managers who came up through role): %s", person_past_titles)

    # 6g. Apollo seniority filter — never sent before; observed Operations Managers
    # and Junior Data Scientists slipping through because of this gap.
    person_seniorities = _seniorities_for_experience_level(exp_level)
    logger.info("[LeadSearch] person_seniorities: %s (exp_level=%s)", person_seniorities, exp_level)

    # ── Build filter ─────────────────────────────────────────────────────
    filters = LeadFilter(
        target_segments=all_segments,
        person_titles_exclude=person_titles_exclude,
        person_locations=normalized_locs,
        organization_locations=normalized_locs,
        organization_industries=normalized_industries,
        email_status=["verified"],  # Always require verified emails
        q_organization_job_titles=q_org_job_titles,
        organization_job_posted_at_range=org_job_posted_at_range,
        q_organization_keyword_tags=niche_keywords,
        currently_using_any_of_technology_uids=tech_stack,
        organization_job_locations=org_job_locations,
        person_past_titles=person_past_titles,
        person_seniorities=person_seniorities,
    )
    logger.info("Production filters generated successfully (clarity=%s)", clarity)
    return filters