"""Lead Collector Engine.

Coordinates the Apollo API to discover leads based on target filters.
Fetches, deduplicates, and permanently stores matched candidates
into the internal PostgreSQL database without revealing emails.

PROGRESSIVE LOOSENING: If Apollo returns 0 results, the collector
automatically loosens filters in priority order:
  1. Remove industry filter
  2. Remove organization_locations (keep person_locations)
  3. Remove person_locations
  4. Remove company size constraints (flatten to single segment)
  5. Use fallback broad titles
"""

from typing import Dict, Any, List, Optional
from copy import deepcopy

from sqlalchemy.orm import Session
from sqlalchemy import or_

from job_outreach_tool.services.shared.schemas.filter_schema import LeadFilter
from job_outreach_tool.services.shared.schemas.target_segment_schema import TargetSegment
from job_outreach_tool.services.lead_discovery.apollo_query_builder import build_apollo_query
from job_outreach_tool.services.lead_discovery.apollo_service import search_people_chunked
from job_outreach_tool.database.models import Lead
from job_outreach_tool.core.logger import get_logger

logger = get_logger(__name__)

# Broad fallback titles that will always find results on Apollo
FALLBACK_BROAD_TITLES = [
    "Engineering Manager", "Product Manager", "Head of Engineering",
    "Director of Engineering", "Tech Lead", "Senior Software Engineer",
    "VP of Engineering", "CTO", "Head of Product", "Software Engineer",
    "Senior Engineer", "Technical Lead", "Development Manager",
    "Program Manager", "Project Manager",
]


def parse_apollo_person(person: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a raw person dictionary from Apollo into internal schema."""

    first_name = person.get("first_name") or ""
    last_name = person.get("last_name") or ""
    if not last_name:
        obfuscated = person.get("last_name_obfuscated") or ""
        if "*" not in obfuscated:
            last_name = obfuscated

    name = f"{first_name} {last_name}".strip()

    if not name:
        name = "Unknown Contact"

    organization = person.get("organization") or {}
    company = organization.get("name") or "Unknown Company"

    title = person.get("title") or "Unknown Title"
    linkedin_url = person.get("linkedin_url")
    apollo_person_id = person.get("id")
    email = person.get("email")

    # Extract location
    city = person.get("city") or ""
    state = person.get("state") or ""
    country = person.get("country") or ""
    location_parts = [p for p in [city, state, country] if p]
    location = ", ".join(location_parts) if location_parts else None

    # Extract industry
    industry = organization.get("industry") or None

    # Extract company size
    num_employees = organization.get("estimated_num_employees")
    if num_employees is not None:
        if num_employees <= 10:
            company_size = "1-10"
        elif num_employees <= 50:
            company_size = "11-50"
        elif num_employees <= 200:
            company_size = "51-200"
        elif num_employees <= 1000:
            company_size = "201-1000"
        elif num_employees <= 5000:
            company_size = "1001-5000"
        else:
            company_size = "5001-10000"
    else:
        company_size = organization.get("employee_count_range") or None

    return {
        "apollo_person_id": apollo_person_id,
        "name": name,
        "title": title,
        "company": company,
        "linkedin_url": linkedin_url,
        "location": location,
        "industry": industry,
        "company_size": company_size,
        "email": email,
    }


def _build_loosening_stages(filters: LeadFilter) -> List[LeadFilter]:
    """Build a list of progressively looser filter variants.

    Each stage removes one constraint. The collector tries each stage
    in order until Apollo returns results.
    """
    stages = []

    # Stage 0: Original filters (already tried by caller)

    # Stage 1: Remove industry filter
    if filters.organization_industries:
        f = LeadFilter(
            target_segments=filters.target_segments,
            person_titles_exclude=filters.person_titles_exclude,
            person_locations=filters.person_locations,
            organization_locations=filters.organization_locations,
            organization_industries=None,
            email_status=filters.email_status,
        )
        stages.append(f)
        logger.info("[LOOSENING] Stage %d: Remove industry filter", len(stages))

    # Stage 2: Remove org_locations (keep person_locations)
    if filters.organization_locations:
        f = LeadFilter(
            target_segments=filters.target_segments,
            person_titles_exclude=filters.person_titles_exclude,
            person_locations=filters.person_locations,
            organization_locations=None,
            organization_industries=None,
            email_status=filters.email_status,
        )
        stages.append(f)
        logger.info("[LOOSENING] Stage %d: Remove org locations + industry", len(stages))

    # Stage 3: Remove person_locations too
    if filters.person_locations:
        f = LeadFilter(
            target_segments=filters.target_segments,
            person_titles_exclude=filters.person_titles_exclude,
            person_locations=[],
            organization_locations=None,
            organization_industries=None,
            email_status=filters.email_status,
        )
        stages.append(f)
        logger.info("[LOOSENING] Stage %d: Remove all location + industry filters", len(stages))

    # Stage 4: Flatten company sizes into one broad segment
    all_titles = []
    seen = set()
    for seg in filters.target_segments:
        for t in seg.person_titles:
            if t not in seen:
                seen.add(t)
                all_titles.append(t)

    f = LeadFilter(
        target_segments=[TargetSegment(company_size_range="1,10000", person_titles=all_titles)],
        person_titles_exclude=filters.person_titles_exclude,
        person_locations=filters.person_locations,
        organization_locations=None,
        organization_industries=None,
        email_status=filters.email_status,
    )
    stages.append(f)
    logger.info("[LOOSENING] Stage %d: Flatten to single segment + remove industry", len(stages))

    # Stage 5: Fallback broad titles, no location/industry
    f = LeadFilter(
        target_segments=[TargetSegment(company_size_range="1,10000", person_titles=list(FALLBACK_BROAD_TITLES))],
        person_titles_exclude=filters.person_titles_exclude,
        person_locations=filters.person_locations,
        organization_locations=None,
        organization_industries=None,
        email_status=None,
    )
    stages.append(f)
    logger.info("[LOOSENING] Stage %d: Fallback broad titles + location only", len(stages))

    # Stage 6: Nuclear — fallback titles, no constraints at all
    f = LeadFilter(
        target_segments=[TargetSegment(company_size_range="1,10000", person_titles=list(FALLBACK_BROAD_TITLES))],
        person_titles_exclude=filters.person_titles_exclude,
        person_locations=[],
        organization_locations=None,
        organization_industries=None,
        email_status=None,
    )
    stages.append(f)
    logger.info("[LOOSENING] Stage %d: Nuclear fallback — no constraints", len(stages))

    return stages


def _try_collect_page(apollo_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Execute one Apollo search and return the people array."""
    try:
        api_response = search_people_chunked(apollo_payload)
        return api_response.get("people", [])
    except Exception as e:
        logger.error("Apollo API error: %s", e, exc_info=True)
        return []


def collect_leads(filters: LeadFilter, candidate_id: int, target_leads: int, db: Session) -> int:
    """Execute iterative Apollo search logic until target_leads are secured.

    If the initial filters return 0 results, progressively loosens
    constraints until results are found.
    """
    # First attempt with original filters
    logger.info("Collecting leads — initial attempt with original filters")
    apollo_payload = build_apollo_query(filters, page=1)
    people = _try_collect_page(apollo_payload)

    # If 0 results, try progressive loosening
    if not people:
        logger.warning("[LOOSENING] Original filters returned 0 results. Starting progressive loosening.")
        loosening_stages = _build_loosening_stages(filters)

        for stage_idx, loose_filters in enumerate(loosening_stages, 1):
            logger.info("[LOOSENING] Trying stage %d/%d", stage_idx, len(loosening_stages))
            apollo_payload = build_apollo_query(loose_filters, page=1)
            people = _try_collect_page(apollo_payload)

            if people:
                logger.info("[LOOSENING] Stage %d returned %d people! Using these filters.", stage_idx, len(people))
                filters = loose_filters  # Use these looser filters for pagination
                break
            else:
                logger.info("[LOOSENING] Stage %d still returned 0 results.", stage_idx)

        if not people:
            logger.error("[LOOSENING] All stages exhausted. No leads found anywhere.")
            return 0

    # Now paginate with the working filters
    leads_collected = 0
    page = 1

    while leads_collected < target_leads:
        if page > 1:
            # We already have people from page 1
            logger.info("Collecting leads - Page %d (Collected: %d/%d)", page, leads_collected, target_leads)
            apollo_payload = build_apollo_query(filters, page=page)
            people = _try_collect_page(apollo_payload)

            if not people:
                logger.info("Apollo search exhausted. No more people returned on page %d.", page)
                break

        for person in people:
            if leads_collected >= target_leads:
                break

            parsed_data = parse_apollo_person(person)

            if not parsed_data["name"]:
                continue

            # Deduplication logic
            apollo_id = parsed_data.get("apollo_person_id")
            linkedin = parsed_data.get("linkedin_url")

            if not apollo_id:
                logger.warning("Person returned without an Apollo ID -> Rejecting.")
                continue

            conditions = [Lead.apollo_id == apollo_id]
            if linkedin:
                conditions.append(Lead.linkedin_url == linkedin)

            # Deduplicate per candidate
            existing = db.query(Lead).filter(
                Lead.candidate_id == candidate_id,
                or_(*conditions)
            ).first()

            if existing:
                logger.debug("Lead skipped, already exists for this candidate (%s).", apollo_id)
                continue

            apollo_email = parsed_data.get("email")

            # Skip leads without verified emails — they cannot be contacted
            if not apollo_email:
                logger.debug("Lead skipped — no verified email: %s at %s",
                            parsed_data.get("name"), parsed_data.get("company"))
                continue

            new_lead = Lead(
                candidate_id=candidate_id,
                apollo_id=apollo_id,
                name=parsed_data.get("name"),
                title=parsed_data.get("title"),
                company=parsed_data.get("company"),
                linkedin_url=linkedin,
                location=parsed_data.get("location"),
                industry=parsed_data.get("industry"),
                company_size=parsed_data.get("company_size"),
                email=apollo_email,
                email_verified=bool(apollo_email),
                status="discovered",
            )

            db.add(new_lead)
            db.flush()

            leads_collected += 1
            logger.debug("Committed new lead %s - %s (%d/%d)", new_lead.id, new_lead.name, leads_collected, target_leads)

        try:
            db.commit()
        except Exception as e:
            logger.error("Failed to commit leads on page %d: %s", page, e, exc_info=True)
            db.rollback()
            break

        page += 1

    logger.info("Lead collection finalised - Total: %d, Target: %d", leads_collected, target_leads)
    return leads_collected