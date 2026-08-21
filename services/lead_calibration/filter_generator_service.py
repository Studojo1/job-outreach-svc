"""Service for generating segmented lead search filters (production grade).

Two code paths:
  1. LLM path (preferred) — uses Career Strategist output (search_strategy) to build
     Apollo filters directly from an AI-generated search strategy. No rules-based title
     lookup. Fully Apollo-validated before use.
  2. Rules-based fallback — the original 5-layer pipeline (role→classify→title_family→
     seniority→expand). Used when Career Strategist is unavailable or fails.

Apollo-normalized industry names and location formats are applied in both paths.
"""

from datetime import datetime, timedelta
from typing import List

from sqlalchemy.orm import Session

from services.shared.schemas.candidate_schema import CandidateProfile
from services.shared.schemas.filter_schema import LeadFilter
from services.shared.schemas.target_segment_schema import TargetSegment
from services.shared.decision_maker_engine import generate_titles_by_company_size
from services.shared.apollo_normalizer import normalize_industries, normalize_locations
from services.candidate_intelligence.career_strategist import (
    APOLLO_VALID_SENIORITY_CODES,
    APOLLO_VALID_SIZE_RANGES,
)
from core.logger import get_logger

logger = get_logger(__name__)

# Per Phase A audit (May 2026): organization_industries dropped from default —
# kept off unless caller explicitly passes industries. Niche keywords
# (q_organization_keyword_tags) are the preferred vertical filter.
APPLY_INDUSTRY_FILTER_BY_DEFAULT = False

# Default job-posting recency window.
JOB_POSTED_RECENCY_DAYS = 30

# Default exclusion titles — roles we should never target as hiring managers.
DEFAULT_EXCLUSION_TITLES = [
    # Recruitment / HR
    "Recruiter",
    "Talent Acquisition",
    "People Operations",
    # Sub-grad ranks
    "Intern",
    "Student",
    "Apprentice",
    "Trainee",
    # Cross-functional noise
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
    "HR Manager",
]

# Used only when rules-based fallback also produces nothing.
FALLBACK_TITLES = [
    "Operations Manager", "Head of Operations", "Director of Operations",
    "Chief of Staff", "General Manager", "Business Operations Manager",
]


def _today_iso() -> str:
    return datetime.utcnow().date().isoformat()


def _days_ago_iso(days: int) -> str:
    return (datetime.utcnow().date() - timedelta(days=days)).isoformat()


def _seniorities_for_experience_level(exp_level: str) -> List[str]:
    """Map internal experience level to Apollo person_seniorities codes (rules-based fallback)."""
    if exp_level == "senior":
        return ["owner", "founder", "c_suite", "partner", "vp", "head", "director", "manager"]
    if exp_level == "mid":
        return ["vp", "head", "director", "manager"]
    # entry / student / graduate — still include head/director who manage juniors
    return ["head", "director", "manager"]


# ─────────────────────────────────────────────────────────────────────────────
# LLM path: build filters from Career Strategist search_strategy
# ─────────────────────────────────────────────────────────────────────────────

def _build_segments_from_strategy(strategy: dict) -> List[TargetSegment]:
    """Convert Career Strategist title_clusters into TargetSegment list.

    Only includes size ranges listed in strategy.target_size_ranges.
    Deduplicates titles across clusters for the same size range.
    """
    target_sizes = set(strategy.get("target_size_ranges") or []) & APOLLO_VALID_SIZE_RANGES
    if not target_sizes:
        target_sizes = APOLLO_VALID_SIZE_RANGES  # fallback: all sizes

    # Merge all clusters into per-size title lists (preserving priority order)
    size_titles: dict[str, list[str]] = {s: [] for s in target_sizes}
    seen: dict[str, set] = {s: set() for s in target_sizes}

    clusters = sorted(
        strategy.get("title_clusters") or [],
        key=lambda c: int(c.get("priority") or 99),
    )
    for cluster in clusters:
        for size, titles in (cluster.get("titles_by_size") or {}).items():
            if size not in target_sizes:
                continue
            for title in titles:
                t = title.strip()
                if t and t not in seen[size]:
                    seen[size].add(t)
                    size_titles[size].append(t)

    segments = [
        TargetSegment(company_size_range=size, person_titles=titles)
        for size, titles in size_titles.items()
        if titles
    ]
    return segments


def _generate_filters_from_strategy(
    strategy: dict,
    candidate_profile: CandidateProfile,
) -> LeadFilter:
    """Build a LeadFilter from the Career Strategist output.

    All strategy values are pre-validated by career_strategist._validate_strategy().
    This function only applies Apollo-safe transformations.
    """
    roles = candidate_profile.preferred_roles
    company_prefs = candidate_profile.company_preferences or {}
    work_prefs = candidate_profile.work_preferences or {}
    work_mode = (work_prefs.get("work_mode") or "").lower()
    clarity = candidate_profile.clarity_score or 0

    # ── Segments from LLM clusters ────────────────────────────────────────
    all_segments = _build_segments_from_strategy(strategy)
    if not all_segments:
        logger.warning("[FilterGen/LLM] No valid segments from strategy — using fallback segment")
        all_segments = [TargetSegment(company_size_range="1,10000", person_titles=list(FALLBACK_TITLES))]

    logger.info("[FilterGen/LLM] Segments from strategy: %d", len(all_segments))
    for seg in all_segments:
        logger.info("  Segment '%s': %d titles — %s", seg.company_size_range, len(seg.person_titles), seg.person_titles[:5])

    # ── Seniority from strategy (already validated against APOLLO_VALID_SENIORITY_CODES) ──
    person_seniorities = [
        s for s in (strategy.get("person_seniorities") or [])
        if s in APOLLO_VALID_SENIORITY_CODES
    ]
    if not person_seniorities:
        person_seniorities = ["vp", "head", "director", "manager"]
    logger.info("[FilterGen/LLM] person_seniorities: %s", person_seniorities)

    # ── Keywords: strategy keywords take priority, but respect clarity gate ──
    niche_keywords = None
    strategy_kws = [k for k in (strategy.get("keyword_strategy") or []) if k][:3]
    if strategy_kws:
        niche_keywords = strategy_kws
    elif clarity >= 0:
        # Fall back to quiz niche_keywords if strategy didn't produce any
        raw_niches = company_prefs.get("niche_keywords") or []
        if raw_niches:
            niche_keywords = [k.strip().lower() for k in raw_niches if k and k.strip()][:3] or None
    if niche_keywords:
        logger.info("[FilterGen/LLM] q_organization_keyword_tags: %s", niche_keywords)

    # ── Location ──────────────────────────────────────────────────────────
    raw_locations = candidate_profile.location_preferences or []
    normalized_locs = normalize_locations(raw_locations)
    logger.info("[FilterGen/LLM] Locations: raw=%s normalized=%s", raw_locations, normalized_locs)

    # ── Currently hiring for (paired with recency window) ─────────────────
    q_org_job_titles = list(roles)[:8] if roles else None
    org_job_posted_at_range = None
    if q_org_job_titles:
        org_job_posted_at_range = {
            "min": _days_ago_iso(JOB_POSTED_RECENCY_DAYS),
            "max": _today_iso(),
        }

    # ── Tech stack (clarity-gated) ────────────────────────────────────────
    tech_stack = None
    if clarity >= 3 and candidate_profile.tech_stack:
        tech_stack = [t.strip().lower().replace(" ", "_") for t in candidate_profile.tech_stack if t.strip()][:3] or None

    # ── Job posting location (in-office / hybrid only) ────────────────────
    org_job_locations = None
    if work_mode in ("onsite", "hybrid") and normalized_locs:
        org_job_locations = normalized_locs

    # ── Person past titles (roles the candidate has held) ─────────────────
    person_past_titles = list(roles)[:5] if roles else None

    return LeadFilter(
        target_segments=all_segments,
        person_titles_exclude=list(DEFAULT_EXCLUSION_TITLES),
        person_locations=normalized_locs,
        organization_locations=normalized_locs,
        organization_industries=None,  # dropped per Phase A audit
        email_status=["verified"],
        q_organization_job_titles=q_org_job_titles,
        organization_job_posted_at_range=org_job_posted_at_range,
        q_organization_keyword_tags=niche_keywords,
        currently_using_any_of_technology_uids=tech_stack,
        organization_job_locations=org_job_locations,
        person_past_titles=person_past_titles,
        person_seniorities=person_seniorities,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Rules-based fallback path (original logic, preserved verbatim)
# ─────────────────────────────────────────────────────────────────────────────

def _generate_filters_rules_based(
    candidate_profile: CandidateProfile,
) -> LeadFilter:
    """Original rules-based filter generation. Used when Career Strategist is unavailable."""
    roles = candidate_profile.preferred_roles
    company_prefs = candidate_profile.company_preferences or {}
    work_prefs = candidate_profile.work_preferences or {}
    work_mode = (work_prefs.get("work_mode") or "").lower()
    clarity = candidate_profile.clarity_score or 0

    exp_level_map = {
        "student": "entry", "grad": "entry", "graduate": "entry", "entry": "entry",
        "switching": "mid", "career_switching": "mid", "experienced": "senior",
    }
    exp_level = exp_level_map.get((candidate_profile.experience_level or "entry").lower(), "entry")
    logger.info("[FilterGen/Rules] Experience level: %s → targeting %s seniority", candidate_profile.experience_level, exp_level)

    # Build segments from decision maker engine
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
                all_segments.append(TargetSegment(company_size_range=seg.company_size_range, person_titles=merged_titles))

    # Restrict segments by quiz company_stage
    stage_raw = str(
        (company_prefs.get("company_stage") or ["any"])[0]
        if isinstance(company_prefs.get("company_stage"), list)
        else (company_prefs.get("company_stage") or "any")
    ).lower()

    wants_any = any(kw in stage_raw for kw in ("no strong", "any", "open to all", "other"))
    if not wants_any and all_segments:
        allowed: set[str] = set()
        if any(kw in stage_raw for kw in ("early", "seed", "under 50")):
            allowed.update(["1,50", "51,200"])
        if any(kw in stage_raw for kw in ("growth", "50-500")):
            allowed.update(["51,200", "201,1000"])
        if any(kw in stage_raw for kw in ("mid-size", "500-2000")):
            allowed.update(["201,1000", "1001,10000"])
        if any(kw in stage_raw for kw in ("large", "enterprise", "mnc", "2000+")):
            allowed.add("1001,10000")
        if allowed:
            restricted = [s for s in all_segments if s.company_size_range in allowed]
            if restricted:
                logger.info(
                    "[FilterGen/Rules] Segment restriction by company_stage=%r: %d → %d segments",
                    stage_raw[:50], len(all_segments), len(restricted),
                )
                all_segments = restricted

    if not all_segments:
        all_segments = [TargetSegment(company_size_range="1,10000", person_titles=list(FALLBACK_TITLES))]
        logger.info("[FilterGen/Rules] No segments generated, using fallback")

    # Location
    raw_locations = candidate_profile.location_preferences or []
    normalized_locs = normalize_locations(raw_locations)

    # Industries
    normalized_industries = None
    if APPLY_INDUSTRY_FILTER_BY_DEFAULT:
        raw_industries = company_prefs.get("industries") or []
        normalized_industries = normalize_industries(raw_industries) or None

    # Currently hiring for + recency
    q_org_job_titles = list(roles)[:8] if roles else None
    org_job_posted_at_range = None
    if q_org_job_titles:
        org_job_posted_at_range = {
            "min": _days_ago_iso(JOB_POSTED_RECENCY_DAYS),
            "max": _today_iso(),
        }

    # Niche keywords
    niche_keywords = None
    if clarity >= 0:
        raw_niches = company_prefs.get("niche_keywords") or []
        if raw_niches:
            niche_keywords = [k.strip().lower() for k in raw_niches if k and k.strip()][:3] or None

    # Tech stack
    tech_stack = None
    if clarity >= 3 and candidate_profile.tech_stack:
        tech_stack = [t.strip().lower().replace(" ", "_") for t in candidate_profile.tech_stack if t.strip()][:3] or None

    # Job posting location
    org_job_locations = None
    if work_mode in ("onsite", "hybrid") and normalized_locs:
        org_job_locations = normalized_locs

    # Past titles
    person_past_titles = list(roles)[:5] if roles else None

    # Seniority
    person_seniorities = _seniorities_for_experience_level(exp_level)

    return LeadFilter(
        target_segments=all_segments,
        person_titles_exclude=list(DEFAULT_EXCLUSION_TITLES),
        person_locations=normalized_locs,
        organization_locations=normalized_locs,
        organization_industries=normalized_industries,
        email_status=["verified"],
        q_organization_job_titles=q_org_job_titles,
        organization_job_posted_at_range=org_job_posted_at_range,
        q_organization_keyword_tags=niche_keywords,
        currently_using_any_of_technology_uids=tech_stack,
        organization_job_locations=org_job_locations,
        person_past_titles=person_past_titles,
        person_seniorities=person_seniorities,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def generate_apollo_filters(
    candidate_profile: CandidateProfile,
    db: Session,
    search_strategy: dict | None = None,
) -> LeadFilter:
    """Convert a candidate profile into production-grade segmented Apollo filters.

    If search_strategy (from Career Strategist) is provided, uses the LLM path.
    Otherwise falls back to the rules-based 5-layer pipeline.
    """
    logger.info(
        "Generating Apollo filters for user_id=%s name=%s path=%s",
        candidate_profile.user_id,
        candidate_profile.name,
        "llm" if search_strategy else "rules",
    )

    if search_strategy:
        try:
            filters = _generate_filters_from_strategy(search_strategy, candidate_profile)
            logger.info("[FilterGen] LLM path complete")
            return filters
        except Exception as exc:
            logger.warning("[FilterGen] LLM path failed (%s) — falling back to rules-based", exc)

    logger.info("[FilterGen] Using rules-based path")
    filters = _generate_filters_rules_based(candidate_profile)
    logger.info("[FilterGen] Rules-based path complete")
    return filters
