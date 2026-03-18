"""Service for generating segmented lead search filters (production grade).

Includes Apollo-normalized industry names and location formats.
"""

from typing import List, Optional

from sqlalchemy.orm import Session

from services.shared.schemas.candidate_schema import CandidateProfile
from services.shared.schemas.filter_schema import LeadFilter
from services.shared.schemas.target_segment_schema import TargetSegment
from services.shared.decision_maker_engine import generate_titles_by_company_size
from services.shared.apollo_normalizer import normalize_industries, normalize_locations
from core.logger import get_logger

logger = get_logger(__name__)

FALLBACK_TITLES = [
    "Engineering Manager", "Product Manager", "Head of Engineering",
    "Director of Engineering", "Tech Lead", "Senior Software Engineer",
    "VP of Engineering", "CTO", "Head of Product",
]

# Default exclusion titles — roles we should never target
DEFAULT_EXCLUSION_TITLES = [
    "Recruiter",
    "HR Manager",
    "Talent Acquisition",
    "People Operations",
    "Intern",
    "Student",
]


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

    # ── Step 5: Industries (NORMALIZED) ──────────────────────────────────
    company_prefs = candidate_profile.company_preferences or {}
    raw_industries = company_prefs.get("industries") or []
    normalized_industries = normalize_industries(raw_industries)
    logger.info("[LeadSearch] Industry filter: raw=%s, normalized=%s", raw_industries, normalized_industries)

    # ── Build filter ─────────────────────────────────────────────────────
    filters = LeadFilter(
        target_segments=all_segments,
        person_titles_exclude=person_titles_exclude,
        person_locations=normalized_locs,
        organization_locations=normalized_locs,
        organization_industries=normalized_industries,
        email_status=["verified"],  # Always require verified emails
    )
    logger.info("Production filters generated successfully")
    return filters