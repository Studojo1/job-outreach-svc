"""Company size segmentation and seniority targeting service."""

from typing import Dict, List

from core.logger import get_logger

logger = get_logger(__name__)

DEFAULT_SEGMENTS: List[str] = [
    "1,50",
    "51,200",
    "201,1000",
    "1001,10000",
]

# Upper bound of employee range → seniority levels to target.
# Broader seniority targeting = more results from Apollo.
_SIZE_SENIORITY_MAP: Dict[int, List[str]] = {
    50: ["head", "director", "lead", "manager", "vp"],
    200: ["manager", "director", "lead", "senior", "head"],
    1000: ["manager", "lead", "senior", "director"],
    10000: ["manager", "lead", "senior"],
}
_DEFAULT_SENIORITY: List[str] = ["manager", "lead", "senior"]


def get_company_size_segments() -> List[str]:
    """Return the standard company-size ranges used for segmented targeting."""
    logger.info("Returning %d company size segments", len(DEFAULT_SEGMENTS))
    return list(DEFAULT_SEGMENTS)


def get_target_seniority_by_size(size: str) -> List[str]:
    """Return the optimal seniority levels for a given employee-count range."""
    try:
        parts = size.split(",")
        upper = int(parts[-1].strip())
    except (ValueError, IndexError):
        logger.warning("Could not parse company size '%s', using defaults", size)
        return list(_DEFAULT_SENIORITY)

    for threshold in sorted(_SIZE_SENIORITY_MAP.keys()):
        if upper <= threshold:
            seniorities = _SIZE_SENIORITY_MAP[threshold]
            logger.info("Company size '%s' (upper=%d) → seniority targets %s", size, upper, seniorities)
            return list(seniorities)

    logger.info("Company size '%s' (upper=%d) above all thresholds → seniority targets %s", size, upper, _DEFAULT_SENIORITY)
    return list(_DEFAULT_SENIORITY)