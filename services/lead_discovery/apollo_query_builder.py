"""Apollo Query Builder Service.

Transforms internal filter domain models into Apollo's specific
people search API JSON payloads.
"""

from typing import Dict, Any

from job_outreach_tool.services.shared.schemas.filter_schema import LeadFilter
from job_outreach_tool.core.logger import get_logger

logger = get_logger(__name__)


def build_apollo_query(filters: LeadFilter, page: int = 1) -> Dict[str, Any]:
    """Convert LeadFilter object into an Apollo Search API payload.

    Args:
        filters: LeadFilter containing targeting parameters.
        page: The pagination index to fetch from Apollo.

    Returns:
        A dictionary ready to be sent as JSON to the Apollo API.
    """
    logger.info("Building Apollo query payload for page %d", page)

    # Flatten the target segments into a unified list of titles and company sizes for this query
    titles = []
    sizes = []
    if getattr(filters, "target_segments", None):
        for seg in filters.target_segments:
            titles.extend(seg.person_titles)
            sizes.append(seg.company_size_range)
    
    # Deduplicate lists
    titles = list(set(titles))
    sizes = list(set(sizes))

    # Construct the payload according to Apollo specs
    payload: Dict[str, Any] = {
        "person_titles": titles,
        "person_locations": filters.person_locations,
        "organization_locations": filters.organization_locations or [],
        "organization_num_employees_ranges": sizes,
        "organization_industries": filters.organization_industries or [],
        "page": page,
        "per_page": 100,  # Max allowed by Apollo per page
    }

    # ALWAYS enforce verified emails at the Apollo API level
    email_status = filters.email_status or ["verified"]
    payload["contact_email_status"] = email_status
    logger.info("[LeadSearch] Email status filter enforced: %s", email_status)

    # Add optional exclusion list if provided
    if filters.person_titles_exclude:
        payload["person_not_titles"] = filters.person_titles_exclude

    # ── Apollo query guardrails ──────────────────────────────────────────
    # Note: Do NOT trim person_titles here. The search_people_chunked()
    # function in apollo_service.py handles automatic chunking for large
    # title lists (splits at 150 per request). Trimming here would silently
    # drop most titles and return zero results for broad searches.
    person_titles = payload.get("person_titles", [])

    if len(person_titles) > 150:
        logger.info("[APOLLO] %d titles will be chunked by search_people_chunked()", len(person_titles))

    # ── Location filter safeguard ────────────────────────────────────────
    if not payload.get("person_locations") and not payload.get("organization_locations"):
        logger.warning("[LeadSearch] WARNING: No location filter applied — leads will be global!")
    else:
        logger.info("[LeadSearch] Location filter applied: %s", payload.get("person_locations"))

    logger.info("[LeadSearch] Apollo query: titles=%d, person_locations=%s, org_locations=%s, industries=%s",
               len(payload.get("person_titles", [])), payload.get("person_locations"),
               payload.get("organization_locations"), payload.get("organization_industries"))

    return payload
