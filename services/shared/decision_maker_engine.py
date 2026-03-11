"""Decision Maker Intelligence Engine (production grade).

5-layer pipeline:
    1. Role → function classification
    2. Function → title family
    3. Company size → seniority levels
    4. Title family → seniority-filtered titles
    5. Title expansion
"""

from typing import List

from services.shared.role_classifier_service import classify_role_function
from services.shared.title_family_service import get_title_family, filter_titles_by_seniority
from services.shared.company_size_service import get_company_size_segments, get_target_seniority_by_size
from services.shared.title_expansion_service import expand_titles
from services.shared.schemas.target_segment_schema import TargetSegment
from core.logger import get_logger

logger = get_logger(__name__)


def generate_decision_maker_titles(role: str, company_size: str = "") -> List[str]:
    """Single-segment title generation (convenience wrapper).

    Args:
        role: Target role string.
        company_size: Employee-count range, e.g. ``"1,50"``.

    Returns:
        Expanded list of decision-maker titles.
    """
    function = classify_role_function(role)
    title_family = get_title_family(function)

    seniority_levels = get_target_seniority_by_size(company_size) if company_size else ["manager", "lead"]
    titles = filter_titles_by_seniority(title_family, seniority_levels)

    if not titles:
        titles = list(title_family.keys())

    titles = expand_titles(titles)
    logger.info("Decision-maker titles for role='%s', size='%s': %d titles", role, company_size, len(titles))
    return titles


def generate_titles_by_company_size(role: str, experience_level: str = "entry") -> List[TargetSegment]:
    """Generate per-segment title lists for a role across all company sizes.

    Adapts title seniority based on candidate experience level:
    - entry (student/grad): junior/manager titles targeting people who manage juniors
    - mid (career_switching): manager/lead/director titles
    - senior (experienced): all titles including VP/C-level

    Pipeline per segment:
        1. Classify role → function
        2. Load title family for function
        3. Determine seniority levels for this company size + experience level
        4. Filter titles by seniority
        5. Expand titles with prefix variations

    Args:
        role: The candidate's target role.
        experience_level: Candidate's career stage (entry/mid/senior).

    Returns:
        A list of ``TargetSegment`` objects, one per company-size range.
    """
    # Layer 1 — classify role
    function = classify_role_function(role)
    logger.info("Layer 1 — Role '%s' → function '%s' (exp_level=%s)", role, function, experience_level)

    # Layer 2 — load title family
    title_family = get_title_family(function)
    logger.info("Layer 2 — Title family for '%s': %d base titles", function, len(title_family))

    # Layer 3-5 per segment
    segments: List[TargetSegment] = []
    for size_range in get_company_size_segments():
        # Layer 3 — seniority levels for this size, adjusted for candidate experience
        base_seniority = get_target_seniority_by_size(size_range)

        # Adapt seniority based on candidate experience level
        if experience_level in ["student", "graduate", "entry"]:
            # Entry-level: focus on junior/manager/lead (exclude director/vp/chief)
            seniority_levels = [s for s in base_seniority if s not in ["vp", "director", "chief"]]
            if not seniority_levels:  # Fallback if all filtered out
                seniority_levels = ["manager", "lead"]
        elif experience_level == "career_switching":
            # Mid-level: include manager/lead/director (exclude vp/chief)
            seniority_levels = [s for s in base_seniority if s not in ["vp", "chief"]]
            if not seniority_levels:
                seniority_levels = ["manager", "lead", "director"]
        else:
            # Experienced: use all available seniority levels
            seniority_levels = base_seniority

        logger.info("Layer 3 — Segment '%s' → seniority %s (adjusted for %s)", size_range, seniority_levels, experience_level)

        # Layer 4 — filter titles
        filtered = filter_titles_by_seniority(title_family, seniority_levels)
        if not filtered:
            filtered = list(title_family.keys())
        logger.info("Layer 4 — Segment '%s': %d filtered titles", size_range, len(filtered))

        # Layer 5 — expand titles
        expanded = expand_titles(filtered)
        logger.info("Layer 5 — Segment '%s': %d expanded titles", size_range, len(expanded))

        segments.append(TargetSegment(company_size_range=size_range, person_titles=expanded))

    logger.info("Generated %d segments for role '%s'", len(segments), role)
    return segments


def generate_titles(candidate_profile) -> List[TargetSegment]:
    """Generate segmented titles from a full candidate profile.

    Convenience wrapper that extracts preferred_roles and merges
    the per-role segments into a single deduplicated list.

    Args:
        candidate_profile: CandidateProfile instance.

    Returns:
        List of TargetSegment objects covering all roles.
    """
    all_segments: List[TargetSegment] = []
    seen = set()

    for role in candidate_profile.preferred_roles:
        for seg in generate_titles_by_company_size(role):
            for title in seg.person_titles:
                key = (seg.company_size_range, title)
                if key not in seen:
                    seen.add(key)
                    existing = next(
                        (s for s in all_segments if s.company_size_range == seg.company_size_range),
                        None,
                    )
                    if existing:
                        existing.person_titles.append(title)
                    else:
                        all_segments.append(
                            TargetSegment(
                                company_size_range=seg.company_size_range,
                                person_titles=[title],
                            )
                        )

    logger.info("generate_titles produced %d segments for %s", len(all_segments), candidate_profile.preferred_roles)
    return all_segments
