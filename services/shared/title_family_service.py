"""Title family service.

Provides function-specific title dictionaries using REAL titles
that people actually use on LinkedIn/Apollo. No fabricated compounds.
"""

from typing import Dict, List

from job_outreach_tool.core.logger import get_logger

logger = get_logger(__name__)

# Each title is tagged with a seniority keyword for downstream filtering.
# These are REAL titles commonly found in Apollo/LinkedIn — no fabricated compounds.
_TITLE_FAMILIES: Dict[str, Dict[str, str]] = {
    "product": {
        "Product Manager": "manager",
        "Senior Product Manager": "senior",
        "Lead Product Manager": "lead",
        "Principal Product Manager": "principal",
        "Group Product Manager": "group",
        "Head of Product": "head",
        "Director of Product": "director",
        "VP of Product": "vp",
        "Associate Product Manager": "junior",
        "Technical Program Manager": "manager",
        "Program Manager": "manager",
    },
    "data": {
        "Data Analyst": "junior",
        "Senior Data Analyst": "senior",
        "Data Scientist": "manager",
        "Senior Data Scientist": "senior",
        "Analytics Manager": "manager",
        "Data Science Manager": "manager",
        "Data Engineering Manager": "manager",
        "Head of Data": "head",
        "Head of Analytics": "head",
        "Director of Data": "director",
        "Director of Analytics": "director",
        "VP of Data": "vp",
        "BI Analyst": "junior",
        "Business Intelligence Manager": "manager",
    },
    "engineering": {
        "Software Engineer": "junior",
        "Senior Software Engineer": "senior",
        "Staff Engineer": "senior",
        "Principal Engineer": "principal",
        "Engineering Manager": "manager",
        "Senior Engineering Manager": "senior",
        "Tech Lead": "lead",
        "Engineering Lead": "lead",
        "Head of Engineering": "head",
        "Director of Engineering": "director",
        "VP of Engineering": "vp",
        "CTO": "vp",
        "Development Manager": "manager",
        "Software Development Manager": "manager",
    },
    "business": {
        "Business Analyst": "junior",
        "Senior Business Analyst": "senior",
        "Operations Manager": "manager",
        "Business Operations Manager": "manager",
        "Strategy Manager": "manager",
        "Head of Operations": "head",
        "Head of Strategy": "head",
        "Director of Operations": "director",
        "Director of Strategy": "director",
        "Chief of Staff": "head",
        "VP of Operations": "vp",
        "General Manager": "manager",
    },
    "marketing": {
        "Marketing Manager": "manager",
        "Senior Marketing Manager": "senior",
        "Growth Marketing Manager": "manager",
        "Digital Marketing Manager": "manager",
        "Content Marketing Manager": "manager",
        "Head of Marketing": "head",
        "Head of Growth": "head",
        "Director of Marketing": "director",
        "VP of Marketing": "vp",
        "Marketing Lead": "lead",
        "Growth Manager": "manager",
        "Brand Manager": "manager",
    },
    "design": {
        "UX Designer": "junior",
        "Senior UX Designer": "senior",
        "Product Designer": "junior",
        "Senior Product Designer": "senior",
        "Design Lead": "lead",
        "Head of Design": "head",
        "Director of Design": "director",
        "UX Research Manager": "manager",
        "Design Manager": "manager",
    },
    "security": {
        "Security Engineer": "junior",
        "Senior Security Engineer": "senior",
        "Security Analyst": "junior",
        "Head of Security": "head",
        "CISO": "vp",
        "Director of Security": "director",
        "Security Manager": "manager",
        "Information Security Manager": "manager",
    },
    "blockchain": {
        "Blockchain Developer": "junior",
        "Senior Blockchain Developer": "senior",
        "Smart Contract Developer": "junior",
        "Web3 Developer": "junior",
        "Blockchain Engineer": "junior",
        "Engineering Manager": "manager",
        "Head of Engineering": "head",
        "CTO": "vp",
        "Tech Lead": "lead",
        "Director of Engineering": "director",
        "VP of Engineering": "vp",
    },
}


def get_title_family(function: str) -> Dict[str, str]:
    """Return the title → seniority mapping for a function category.

    Args:
        function: Function category name.

    Returns:
        Dict mapping title strings to their seniority keyword.
        Falls back to ``"business"`` if the function is unknown.
    """
    family = _TITLE_FAMILIES.get(function, _TITLE_FAMILIES["business"])
    logger.info("Title family for '%s': %d titles available", function, len(family))
    return family


def filter_titles_by_seniority(
    title_family: Dict[str, str], seniority_levels: List[str]
) -> List[str]:
    """Filter titles to only those matching the desired seniority levels.

    Args:
        title_family: Dict of title → seniority keyword.
        seniority_levels: Allowed seniority keywords.

    Returns:
        List of matching title strings.
    """
    seniority_set = set(s.lower() for s in seniority_levels)
    matched = [
        title
        for title, seniority in title_family.items()
        if seniority.lower() in seniority_set
    ]
    logger.info(
        "Filtered %d titles down to %d matching seniority levels %s",
        len(title_family), len(matched), seniority_levels,
    )
    return matched