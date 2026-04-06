"""Lead Collector Engine.

Coordinates the Apollo API to discover leads based on target filters.
Fetches, deduplicates, and permanently stores matched candidates
into the internal PostgreSQL database without revealing emails.

PROGRESSIVE LOOSENING: If Apollo returns fewer than target_leads, the
collector automatically loosens filters in priority order:
  1. Remove industry filter
  2. Remove organization_locations (keep person_locations)
  3. Remove person_locations
  4. Remove company size constraints (flatten to single segment)
  5. Use fallback broad titles
  6. Nuclear — fallback titles, no constraints at all
"""

from typing import Dict, Any, List
from sqlalchemy.orm import Session
from sqlalchemy import or_

from services.shared.schemas.filter_schema import LeadFilter
from services.shared.schemas.target_segment_schema import TargetSegment
from services.lead_discovery.apollo_query_builder import build_apollo_query
from services.lead_discovery.apollo_service import search_people_chunked
from database.models import Lead
from core.logger import get_logger

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
    company = organization.get("name") or person.get("organization_name") or "Unknown Company"

    title = person.get("title") or "Unknown Title"
    linkedin_url = person.get("linkedin_url")
    apollo_person_id = person.get("id")
    email = person.get("email")

    # Extract location — mixed_people endpoint may only have has_city booleans
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

    company_description = (organization.get("short_description") or "")[:500] or None

    return {
        "apollo_person_id": apollo_person_id,
        "name": name,
        "title": title,
        "company": company,
        "linkedin_url": linkedin_url,
        "location": location,
        "industry": industry,
        "company_size": company_size,
        "company_description": company_description,
        "email": email,
    }


def _build_loosening_stages(filters: LeadFilter) -> List[LeadFilter]:
    """Build a list of progressively looser filter variants."""
    stages = []

    # Stage 1: Remove industry filter
    if filters.organization_industries:
        stages.append(LeadFilter(
            target_segments=filters.target_segments,
            person_titles_exclude=filters.person_titles_exclude,
            person_locations=filters.person_locations,
            organization_locations=filters.organization_locations,
            organization_industries=None,
            email_status=filters.email_status,
        ))
        logger.info("[LOOSENING] Stage %d: Remove industry filter", len(stages))

    # Stage 2: Remove org_locations (keep person_locations)
    if filters.organization_locations:
        stages.append(LeadFilter(
            target_segments=filters.target_segments,
            person_titles_exclude=filters.person_titles_exclude,
            person_locations=filters.person_locations,
            organization_locations=None,
            organization_industries=None,
            email_status=filters.email_status,
        ))
        logger.info("[LOOSENING] Stage %d: Remove org locations + industry", len(stages))

    # Stage 3: Remove person_locations too
    if filters.person_locations:
        stages.append(LeadFilter(
            target_segments=filters.target_segments,
            person_titles_exclude=filters.person_titles_exclude,
            person_locations=[],
            organization_locations=None,
            organization_industries=None,
            email_status=filters.email_status,
        ))
        logger.info("[LOOSENING] Stage %d: Remove all location + industry filters", len(stages))

    # Stage 4: Flatten company sizes into one broad segment
    all_titles = []
    seen = set()
    for seg in filters.target_segments:
        for t in seg.person_titles:
            if t not in seen:
                seen.add(t)
                all_titles.append(t)

    stages.append(LeadFilter(
        target_segments=[TargetSegment(company_size_range="1,10000", person_titles=all_titles)],
        person_titles_exclude=filters.person_titles_exclude,
        person_locations=filters.person_locations,
        organization_locations=None,
        organization_industries=None,
        email_status=filters.email_status,
    ))
    logger.info("[LOOSENING] Stage %d: Flatten to single segment + remove industry", len(stages))

    # Stage 5: Fallback broad titles, keep location
    stages.append(LeadFilter(
        target_segments=[TargetSegment(company_size_range="1,10000", person_titles=list(FALLBACK_BROAD_TITLES))],
        person_titles_exclude=filters.person_titles_exclude,
        person_locations=filters.person_locations,
        organization_locations=None,
        organization_industries=None,
        email_status=["verified"],
    ))
    logger.info("[LOOSENING] Stage %d: Fallback broad titles + location only", len(stages))

    # Stage 6: Nuclear — fallback titles, no constraints at all
    stages.append(LeadFilter(
        target_segments=[TargetSegment(company_size_range="1,10000", person_titles=list(FALLBACK_BROAD_TITLES))],
        person_titles_exclude=filters.person_titles_exclude,
        person_locations=[],
        organization_locations=None,
        organization_industries=None,
        email_status=["verified"],
    ))
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


MAX_LEADS_PER_COMPANY = 4


def _store_people(
    people: List[Dict[str, Any]],
    candidate_id: int,
    target_leads: int,
    db: Session,
    leads_collected: int,
) -> int:
    """Parse, deduplicate, and store a batch of Apollo people. Returns updated count.

    Enforces a per-company cap of MAX_LEADS_PER_COMPANY to ensure diversity.
    """
    from sqlalchemy import func

    # Build company count map from already-stored leads
    company_counts: Dict[str, int] = {}
    rows = (
        db.query(Lead.company, func.count(Lead.id))
        .filter(Lead.candidate_id == candidate_id)
        .group_by(Lead.company)
        .all()
    )
    for company_name, cnt in rows:
        if company_name:
            company_counts[company_name.lower()] = cnt

    for person in people:
        if leads_collected >= target_leads:
            break

        parsed_data = parse_apollo_person(person)
        if not parsed_data["name"]:
            continue

        apollo_id = parsed_data.get("apollo_person_id")
        linkedin = parsed_data.get("linkedin_url")
        if not apollo_id:
            continue

        # Per-company cap — skip if this company already has enough leads
        company = parsed_data.get("company") or "Unknown Company"
        company_key = company.lower()
        if company_counts.get(company_key, 0) >= MAX_LEADS_PER_COMPANY:
            continue

        conditions = [Lead.apollo_id == apollo_id]
        if linkedin:
            conditions.append(Lead.linkedin_url == linkedin)

        existing = db.query(Lead).filter(
            Lead.candidate_id == candidate_id,
            or_(*conditions)
        ).first()
        if existing:
            continue

        apollo_email = parsed_data.get("email")
        new_lead = Lead(
            candidate_id=candidate_id,
            apollo_id=apollo_id,
            name=parsed_data.get("name"),
            title=parsed_data.get("title"),
            company=company,
            linkedin_url=linkedin,
            location=parsed_data.get("location"),
            industry=parsed_data.get("industry"),
            company_size=parsed_data.get("company_size"),
            company_description=parsed_data.get("company_description"),
            email=apollo_email,
            email_verified=bool(apollo_email),
            status="discovered",
        )
        db.add(new_lead)
        db.flush()
        leads_collected += 1
        company_counts[company_key] = company_counts.get(company_key, 0) + 1

    return leads_collected


def _paginate_filters(
    filters: LeadFilter,
    candidate_id: int,
    target_leads: int,
    db: Session,
    leads_collected: int,
) -> int:
    """Paginate through all Apollo pages for a given filter set.

    Returns cumulative leads_collected.
    """
    page = 1
    while leads_collected < target_leads:
        logger.info("Paginating page %d (collected: %d/%d)", page, leads_collected, target_leads)
        apollo_payload = build_apollo_query(filters, page=page)
        people = _try_collect_page(apollo_payload)

        if not people:
            logger.info("Apollo exhausted on page %d.", page)
            break

        leads_collected = _store_people(people, candidate_id, target_leads, db, leads_collected)

        try:
            db.commit()
        except Exception as e:
            logger.error("Failed to commit leads on page %d: %s", page, e, exc_info=True)
            db.rollback()
            break

        page += 1

    return leads_collected


def collect_leads(filters: LeadFilter, candidate_id: int, target_leads: int, db: Session) -> int:
    """Execute iterative Apollo search logic until target_leads are secured.

    Strategy:
      1. Paginate through Apollo using the original filters.
      2. If pagination exhausts before reaching target_leads, try
         progressive loosening — each stage broadens the search.
      3. Continue paginating with each loosened filter set until the
         target is met or all stages are exhausted.

    This ensures the 500-lead minimum is enforced as long as Apollo
    has enough data across any combination of filters.
    """
    # Phase 1: Original filters — paginate fully
    logger.info("Collecting leads — Phase 1: original filters (target=%d)", target_leads)
    leads_collected = _paginate_filters(filters, candidate_id, target_leads, db, 0)

    logger.info("[PHASE1] Original filters collected %d/%d leads", leads_collected, target_leads)

    # Phase 2: Progressive loosening if we fell short
    if leads_collected < target_leads:
        logger.warning(
            "[LOOSENING] Original filters only produced %d/%d leads. Starting progressive loosening.",
            leads_collected, target_leads,
        )
        loosening_stages = _build_loosening_stages(filters)

        for stage_idx, loose_filters in enumerate(loosening_stages, 1):
            if leads_collected >= target_leads:
                break

            logger.info(
                "[LOOSENING] Stage %d/%d (collected so far: %d/%d)",
                stage_idx, len(loosening_stages), leads_collected, target_leads,
            )

            leads_collected = _paginate_filters(
                loose_filters, candidate_id, target_leads, db, leads_collected,
            )

            logger.info(
                "[LOOSENING] Stage %d result: %d/%d leads",
                stage_idx, leads_collected, target_leads,
            )

    logger.info("Lead collection finalised - Total: %d, Target: %d", leads_collected, target_leads)
    return leads_collected


def collect_dream_company_leads(
    base_filters: LeadFilter,
    dream_companies: list[str],
    candidate_id: int,
    db: Session,
) -> int:
    """Secondary search pass — find leads at the candidate's dream companies.

    For each company (capped at 5), clones the base filters with
    q_organization_name set, and searches 1 page (up to 100 leads).
    Deduplication is handled by _store_people() which checks apollo_id.

    Returns total new leads added across all dream company searches.
    """
    total_added = 0
    companies = [c.strip() for c in dream_companies if c.strip()][:5]

    if not companies:
        return 0

    logger.info("[DREAM] Starting dream company search for %d companies: %s", len(companies), companies)

    for company_name in companies:
        try:
            dream_filters = LeadFilter(
                target_segments=base_filters.target_segments,
                person_titles_exclude=base_filters.person_titles_exclude,
                person_locations=base_filters.person_locations,
                organization_locations=base_filters.organization_locations,
                organization_industries=None,  # don't restrict industry for dream companies
                q_organization_name=company_name,
                email_status=["verified"],
            )
            apollo_payload = build_apollo_query(dream_filters, page=1)
            people = _try_collect_page(apollo_payload)

            if not people:
                logger.info("[DREAM] No results for company '%s'", company_name)
                continue

            before = total_added
            # Use a high target so we store all results from the single page
            current_count = _store_people(people, candidate_id, 10000, db, 0)
            total_added += current_count

            try:
                db.commit()
            except Exception as e:
                logger.error("[DREAM] Failed to commit leads for '%s': %s", company_name, e)
                db.rollback()
                continue

            logger.info("[DREAM] Company '%s': found %d people, stored %d new leads",
                       company_name, len(people), current_count)

        except Exception as e:
            logger.error("[DREAM] Error searching for company '%s': %s", company_name, e, exc_info=True)
            continue

    logger.info("[DREAM] Dream company search complete: %d new leads from %d companies", total_added, len(companies))
    return total_added
