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

# Per-function fallback titles. Picked when progressive loosening can't find
# leads with the candidate's exact title list. Picking software titles for a
# civil engineer or writer is what produced the wrong-leads bug — so each
# function has its own broad-but-on-domain fallback set.
_FUNCTION_FALLBACK_TITLES = {
    "engineering": [
        "Engineering Manager", "Software Engineering Manager", "Head of Engineering",
        "Director of Engineering", "Tech Lead", "Technical Lead", "VP of Engineering",
        "CTO", "Senior Software Engineer", "Software Engineer",
    ],
    "data": [
        "Head of Data", "Director of Data", "Analytics Manager", "Data Science Manager",
        "Head of Analytics", "VP of Data", "Senior Data Analyst", "Data Engineering Manager",
    ],
    "product": [
        "Product Manager", "Senior Product Manager", "Head of Product",
        "Director of Product", "VP of Product", "Group Product Manager",
    ],
    "marketing": [
        "Marketing Manager", "Head of Marketing", "Director of Marketing",
        "Growth Marketing Manager", "Digital Marketing Manager", "VP of Marketing",
        "Brand Manager", "Head of Growth",
    ],
    "design": [
        "Head of Design", "Design Lead", "Design Manager", "Director of Design",
        "VP of Design", "Senior UX Designer", "Senior Product Designer",
    ],
    "civil_construction": [
        "Construction Manager", "Project Manager", "Senior Project Manager",
        "Head of Construction", "Director of Construction", "Civil Engineering Manager",
        "Senior Civil Engineer", "Site Engineer", "Project Engineer",
    ],
    "hr": [
        "HR Manager", "Head of People", "Head of HR", "Director of HR",
        "VP of HR", "People Operations Manager", "Talent Acquisition Manager",
    ],
    "writing_content": [
        "Content Manager", "Head of Content", "Editorial Director",
        "Editor in Chief", "Managing Editor", "Director of Content",
    ],
    "customer_success": [
        "Customer Success Manager", "Head of Customer Success",
        "Director of Customer Success", "VP of Customer Success",
    ],
    "cloud_ops": [
        "DevOps Manager", "Head of Platform", "Head of Infrastructure",
        "Cloud Engineering Manager", "SRE Manager", "Director of Infrastructure",
    ],
    "finance": [
        "Finance Manager", "Head of Finance", "Director of Finance",
        "VP of Finance", "Controller", "FP&A Manager", "CFO",
    ],
    "legal": [
        "Head of Legal", "Legal Director", "General Counsel",
        "Senior Counsel", "VP of Legal",
    ],
    "sales": [
        "Sales Manager", "Head of Sales", "Director of Sales",
        "VP of Sales", "Account Executive", "Account Manager",
    ],
    "operations": [
        "Operations Manager", "Head of Operations", "Director of Operations",
        "VP of Operations", "Business Operations Manager", "COO",
    ],
    "security": [
        "Security Manager", "Head of Security", "Director of Security",
        "Information Security Manager", "CISO",
    ],
    "blockchain": [
        "Engineering Manager", "Head of Engineering", "Senior Blockchain Developer",
        "Director of Engineering",
    ],
    "business": [
        "Operations Manager", "Strategy Manager", "Business Operations Manager",
        "Head of Operations", "Director of Strategy", "Chief of Staff",
    ],
}


def _fallback_titles_for_filter(filters) -> list:
    """Pick broad fallback titles based on the candidate's classified function.

    Inspects the existing filter's title segments to identify the function (by
    looking for keyword overlap with each title family) and returns a sensible
    on-domain fallback list. Defaults to engineering if nothing can be inferred.
    """
    from services.shared.title_family_service import _TITLE_FAMILIES

    titles_seen = set()
    for seg in getattr(filters, "target_segments", []) or []:
        for t in seg.person_titles:
            titles_seen.add(t)

    # Score each function by how many of its family titles appear in the filter
    scores = {}
    for fn, family in _TITLE_FAMILIES.items():
        family_titles = set(family.keys())
        scores[fn] = len(titles_seen & family_titles)

    if not scores:
        return _FUNCTION_FALLBACK_TITLES["engineering"]

    best_fn = max(scores.items(), key=lambda kv: kv[1])[0]
    if scores[best_fn] == 0:
        # Couldn't infer — use generic engineering as a safe default
        return _FUNCTION_FALLBACK_TITLES["engineering"]
    return _FUNCTION_FALLBACK_TITLES.get(best_fn, _FUNCTION_FALLBACK_TITLES["engineering"])


# Backward-compat alias — older code paths still reference this name
FALLBACK_BROAD_TITLES = _FUNCTION_FALLBACK_TITLES["engineering"]


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

    # Capture domain if Apollo's free people search happens to return one.
    # In practice the free /mixed_people/api_search response keeps `primary_domain`
    # paywalled, so this is usually None — the real disambiguation happens
    # later via Apollo /mixed_companies/search in company_enrichment_service.
    company_domain = _extract_domain(organization)

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
        "company_domain": company_domain,
        "email": email,
    }


def _extract_domain(organization: dict) -> str | None:
    """Pull a clean lowercase domain from Apollo organization payload."""
    raw = organization.get("primary_domain") or organization.get("website_url") or ""
    if not raw:
        return None
    raw = raw.strip().lower()
    if raw.startswith(("http://", "https://")):
        raw = raw.split("://", 1)[1]
    raw = raw.split("/", 1)[0]
    if raw.startswith("www."):
        raw = raw[4:]
    return raw or None


def _clone_filters(base: LeadFilter, **overrides) -> LeadFilter:
    """Build a new LeadFilter from `base` with the given fields overridden.
    Preserves all Phase A audit-passed fields unless explicitly nulled."""
    data = base.model_dump() if hasattr(base, "model_dump") else base.dict()
    data.update(overrides)
    return LeadFilter(**data)


def _probe_batch(filters: LeadFilter) -> list[dict]:
    """Fetch Apollo page 1 without storing anything. Returns parsed people dicts.

    Used by quality_probe_loop to evaluate lead quality before the full
    500-lead collection runs.
    """
    from services.lead_discovery.apollo_query_builder import build_apollo_query
    payload = build_apollo_query(filters, page=1)
    people = _try_collect_page(payload)
    return [parse_apollo_person(p) for p in people if p.get("id")]


def quality_probe_loop(
    filters: LeadFilter,
    candidate_prefs: dict,
    max_iterations: int = 3,
) -> tuple[LeadFilter, list[str]]:
    """Probe Apollo, evaluate quality with LLM, adjust filters if needed. Max 3 rounds.

    Runs before the full 500-lead collection. Each iteration:
      1. Fetch page 1 (no DB writes)
      2. Send sample to LLM with candidate profile → get quality score + adjustments
      3. Apply: restrict company sizes, add keywords, inject suggested titles,
         accumulate explicit company exclusions
      4. Stop when quality_score >= 7 or max_iterations reached

    Returns:
        (finalised_filters, explicit_exclusions) where explicit_exclusions is a list
        of company names the LLM flagged as hard mismatches — passed to collect_leads
        to skip them even if Apollo returns them.

    candidate_prefs keys: company_stage, niche_keywords, preferred_roles,
                          archetype_label, company_type_avoid.
    """
    from services.lead_calibration.lead_quality_evaluator import evaluate_probe_with_llm
    from services.candidate_intelligence.career_strategist import APOLLO_VALID_SIZE_RANGES

    all_exclusions: list[str] = []

    for iteration in range(max_iterations + 1):
        probe = _probe_batch(filters)
        logger.info("[QualityProbe] iter=%d probe_size=%d", iteration, len(probe))

        if not probe:
            logger.info("[QualityProbe] Empty probe — skipping quality check, proceeding")
            break

        result = evaluate_probe_with_llm(probe, candidate_prefs)
        quality_score = int(result.get("quality_score") or 7)
        main_issue = result.get("main_issue")

        # Accumulate company exclusions regardless of whether we iterate
        new_exclusions = result.get("explicit_exclusions") or []
        if isinstance(new_exclusions, list):
            for exc_name in new_exclusions:
                if exc_name and exc_name not in all_exclusions:
                    all_exclusions.append(str(exc_name).strip())

        logger.info(
            "[QualityProbe] iter=%d quality=%d/10 issue=%r exclusions=%s",
            iteration, quality_score, main_issue, all_exclusions,
        )

        if quality_score >= 7 or iteration == max_iterations:
            break

        # ── Apply LLM-suggested filter adjustments ───────────────────────
        adjusted = False

        # 1. Restrict company size ranges
        restrict_sizes = result.get("restrict_company_sizes")
        if restrict_sizes and isinstance(restrict_sizes, list):
            valid_sizes = [s for s in restrict_sizes if s in APOLLO_VALID_SIZE_RANGES]
            restricted_segs = [
                s for s in filters.target_segments
                if s.company_size_range in valid_sizes
            ]
            if restricted_segs:
                filters = _clone_filters(filters, target_segments=restricted_segs)
                logger.info("[QualityProbe] Restricted to %d segments: %s", len(restricted_segs), valid_sizes)
                adjusted = True

        # 2. Add keyword tags (cap at 3 total)
        add_kws = result.get("add_keyword_tags")
        if add_kws and isinstance(add_kws, list):
            existing = list(filters.q_organization_keyword_tags or [])
            merged = list(dict.fromkeys(existing + [k.lower().strip() for k in add_kws if k]))[:3]
            if merged != existing:
                filters = _clone_filters(filters, q_organization_keyword_tags=merged or None)
                logger.info("[QualityProbe] Updated keyword tags: %s", merged)
                adjusted = True

        # 3. Inject suggested titles into ALL segments (not size-gated — probe knows best)
        suggest_titles = result.get("suggest_titles")
        if suggest_titles and isinstance(suggest_titles, list):
            clean_titles = [str(t).strip() for t in suggest_titles if t and str(t).strip()][:5]
            if clean_titles:
                updated_segs = []
                for seg in filters.target_segments:
                    existing_titles = list(seg.person_titles)
                    for t in clean_titles:
                        if t not in existing_titles:
                            existing_titles.append(t)
                    updated_segs.append(TargetSegment(
                        company_size_range=seg.company_size_range,
                        person_titles=existing_titles,
                    ))
                filters = _clone_filters(filters, target_segments=updated_segs)
                logger.info("[QualityProbe] Injected suggested titles: %s", clean_titles)
                adjusted = True

        if not adjusted:
            logger.info("[QualityProbe] No actionable adjustments from LLM — stopping early")
            break

    return filters, all_exclusions

    return filters


def _build_loosening_stages(filters: LeadFilter) -> List[LeadFilter]:
    """Build a list of progressively looser filter variants.

    Priority order (drop the high-precision filters first; preserve the
    role-and-location intent as long as possible):

      Stage 1: Drop currently-hiring-for + posting recency (narrowest filters)
      Stage 2: Drop tech_stack
      Stage 3: Drop niche_keywords
      Stage 4: Drop industry filter
      Stage 5: Drop person_past_titles + organization_job_locations
      Stage 6: Drop org_locations (keep person_locations)
      Stage 7: Flatten company sizes into one segment
      Stage 8: Drop person_locations (global search, keep ORIGINAL titles)
      Stage 9: Function-specific fallback titles, KEEP location
      Stage 10: Nuclear — function-specific fallback titles, no constraints

    Never swap titles for software fallback by default — that produced the
    wrong-leads bug for civil engineers / writers. Each function has its
    own fallback set in _FUNCTION_FALLBACK_TITLES.
    """
    stages = []

    # Stage 1: Drop currently-hiring-for + posting recency (drop together —
    # they only make sense paired)
    if filters.q_organization_job_titles or filters.organization_job_posted_at_range:
        stages.append(_clone_filters(filters,
            q_organization_job_titles=None,
            organization_job_posted_at_range=None,
        ))
        logger.info("[LOOSENING] Stage %d: Drop currently-hiring + posting recency", len(stages))

    # Stage 2: Drop tech_stack (high-precision, often sparse on Indian companies)
    if filters.currently_using_any_of_technology_uids:
        stages.append(_clone_filters(filters,
            q_organization_job_titles=None,
            organization_job_posted_at_range=None,
            currently_using_any_of_technology_uids=None,
        ))
        logger.info("[LOOSENING] Stage %d: + drop tech_stack", len(stages))

    # Stage 3: Drop niche_keywords
    if filters.q_organization_keyword_tags:
        stages.append(_clone_filters(filters,
            q_organization_job_titles=None,
            organization_job_posted_at_range=None,
            currently_using_any_of_technology_uids=None,
            q_organization_keyword_tags=None,
        ))
        logger.info("[LOOSENING] Stage %d: + drop niche keywords", len(stages))

    # Stage 4: Drop industry filter
    if filters.organization_industries:
        stages.append(_clone_filters(filters,
            q_organization_job_titles=None,
            organization_job_posted_at_range=None,
            currently_using_any_of_technology_uids=None,
            q_organization_keyword_tags=None,
            organization_industries=None,
        ))
        logger.info("[LOOSENING] Stage %d: + drop industry filter", len(stages))

    # Stage 5: Drop person_past_titles + organization_job_locations
    if filters.person_past_titles or filters.organization_job_locations:
        stages.append(_clone_filters(filters,
            q_organization_job_titles=None,
            organization_job_posted_at_range=None,
            currently_using_any_of_technology_uids=None,
            q_organization_keyword_tags=None,
            organization_industries=None,
            person_past_titles=None,
            organization_job_locations=None,
        ))
        logger.info("[LOOSENING] Stage %d: + drop past_titles + job_locations", len(stages))

    # Stage 6: Drop organization_locations (keep person_locations)
    if filters.organization_locations:
        stages.append(_clone_filters(filters,
            q_organization_job_titles=None,
            organization_job_posted_at_range=None,
            currently_using_any_of_technology_uids=None,
            q_organization_keyword_tags=None,
            organization_industries=None,
            person_past_titles=None,
            organization_job_locations=None,
            organization_locations=None,
        ))
        logger.info("[LOOSENING] Stage %d: + drop org_locations", len(stages))

    # Stage 7: Flatten company sizes into one segment (keep titles + person_location)
    all_titles = []
    seen = set()
    for seg in filters.target_segments:
        for t in seg.person_titles:
            if t not in seen:
                seen.add(t)
                all_titles.append(t)

    stages.append(_clone_filters(filters,
        target_segments=[TargetSegment(company_size_range="1,10000", person_titles=all_titles)],
        q_organization_job_titles=None,
        organization_job_posted_at_range=None,
        currently_using_any_of_technology_uids=None,
        q_organization_keyword_tags=None,
        organization_industries=None,
        person_past_titles=None,
        organization_job_locations=None,
        organization_locations=None,
    ))
    logger.info("[LOOSENING] Stage %d: + flatten company sizes", len(stages))

    # Stage 8: Drop person_locations (keep ORIGINAL titles, global search)
    if filters.person_locations:
        stages.append(_clone_filters(filters,
            target_segments=[TargetSegment(company_size_range="1,10000", person_titles=all_titles)],
            q_organization_job_titles=None,
            organization_job_posted_at_range=None,
            currently_using_any_of_technology_uids=None,
            q_organization_keyword_tags=None,
            organization_industries=None,
            person_past_titles=None,
            organization_job_locations=None,
            organization_locations=None,
            person_locations=[],
        ))
        logger.info("[LOOSENING] Stage %d: + drop person_locations (global search)", len(stages))

    # Stage 9: Function-specific fallback titles, KEEP location
    fallback_titles = _fallback_titles_for_filter(filters)
    stages.append(_clone_filters(filters,
        target_segments=[TargetSegment(company_size_range="1,10000", person_titles=list(fallback_titles))],
        q_organization_job_titles=None,
        organization_job_posted_at_range=None,
        currently_using_any_of_technology_uids=None,
        q_organization_keyword_tags=None,
        organization_industries=None,
        person_past_titles=None,
        organization_job_locations=None,
        organization_locations=None,
        email_status=["verified"],
    ))
    logger.info("[LOOSENING] Stage %d: Function-specific fallback titles + location (titles=%s)",
                len(stages), fallback_titles[:5])

    # Stage 10: Nuclear — function-specific fallback titles, no constraints
    stages.append(_clone_filters(filters,
        target_segments=[TargetSegment(company_size_range="1,10000", person_titles=list(fallback_titles))],
        q_organization_job_titles=None,
        organization_job_posted_at_range=None,
        currently_using_any_of_technology_uids=None,
        q_organization_keyword_tags=None,
        organization_industries=None,
        person_past_titles=None,
        organization_job_locations=None,
        organization_locations=None,
        person_locations=[],
        email_status=["verified"],
    ))
    logger.info("[LOOSENING] Stage %d: Nuclear fallback (function-specific titles, no location)", len(stages))

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
    excluded_companies: list[str] | None = None,
) -> int:
    """Parse, deduplicate, and store a batch of Apollo people. Returns updated count.

    Enforces a per-company cap of MAX_LEADS_PER_COMPANY to ensure diversity.
    excluded_companies: company names flagged by the probe loop LLM as hard mismatches.
    """
    from sqlalchemy import func

    excluded_lower = {c.lower() for c in (excluded_companies or [])}

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

        # Probe-loop exclusion — hard-block companies the LLM flagged as mismatches
        if excluded_lower and any(exc in company_key for exc in excluded_lower):
            logger.info("[STORE] Skipping excluded company: %s", company)
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
            company_domain=parsed_data.get("company_domain"),
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
    excluded_companies: list[str] | None = None,
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

        leads_collected = _store_people(
            people, candidate_id, target_leads, db, leads_collected,
            excluded_companies=excluded_companies,
        )

        try:
            db.commit()
        except Exception as e:
            logger.error("Failed to commit leads on page %d: %s", page, e, exc_info=True)
            db.rollback()
            break

        page += 1

    return leads_collected


def collect_leads(
    filters: LeadFilter,
    candidate_id: int,
    target_leads: int,
    db: Session,
    excluded_companies: list[str] | None = None,
) -> int:
    """Execute iterative Apollo search logic until target_leads are secured.

    Strategy:
      1. Paginate through Apollo using the original filters.
      2. If pagination exhausts before reaching target_leads, try
         progressive loosening — each stage broadens the search.
      3. Continue paginating with each loosened filter set until the
         target is met or all stages are exhausted.

    excluded_companies: company names flagged by probe-loop LLM as hard mismatches —
    skipped even when Apollo returns them during full collection.
    """
    # Phase 1: Original filters — paginate fully
    logger.info("Collecting leads — Phase 1: original filters (target=%d, exclusions=%d)",
                target_leads, len(excluded_companies or []))
    leads_collected = _paginate_filters(
        filters, candidate_id, target_leads, db, 0,
        excluded_companies=excluded_companies,
    )

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
                excluded_companies=excluded_companies,
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
