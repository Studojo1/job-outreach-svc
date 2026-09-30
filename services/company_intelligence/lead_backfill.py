"""Fill a lead's company fields from the cached company profiles (UC-Q03).

Apollo's free people search returns no organization data on our plan, so
every lead was stored with no industry, company size, description and, for
91% of them, no domain (51,317 leads for 63 candidates, 24-28 Sep). The
company_profiles cache (about 90k companies, filled by earlier research runs)
often already knows these. This copies them onto the lead.

Only blank fields are filled: whatever Apollo returned wins. Person-level
fields (the lead's own location, their LinkedIn URL) are not in the company
cache and stay as they are.
"""
import logging
from typing import Dict, Iterable, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from database.models import CompanyProfile
from services.lead_discovery.domain_utils import clean_domain

logger = logging.getLogger(__name__)

_DESCRIPTION_MAX = 500  # same cap parse_apollo_person applies


def profile_by_name(db: Session, name: Optional[str]) -> Optional[CompanyProfile]:
    """The cached profile whose name equals `name`, ignoring case and spaces.

    Compares lower(name) = :name so the expression index
    idx_company_profiles_lower_name (migration 075) serves it. The old
    name ILIKE :name could use no index: 4.1M full scans of the 94k-row table
    by 30 Sep (audit AR-D02). ILIKE also read '_' and '%' in a company name as
    wildcards, so "A_B Labs" matched "AXB Labs".
    """
    key = (name or "").strip().lower()
    if not key:
        return None
    return (
        db.query(CompanyProfile)
        .filter(func.lower(CompanyProfile.name) == key)
        .order_by(CompanyProfile.id)
        .first()
    )


def company_size_bucket(num_employees) -> Optional[str]:
    """Employee count as the size band stored on leads."""
    if num_employees is None:
        return None
    try:
        n = int(num_employees)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if n <= 10:
        return "1-10"
    if n <= 50:
        return "11-50"
    if n <= 200:
        return "51-200"
    if n <= 1000:
        return "201-1000"
    if n <= 5000:
        return "1001-5000"
    return "5001-10000"


def _first_industry(industries) -> Optional[str]:
    if isinstance(industries, str):
        industries = [industries]
    for ind in industries or []:
        if isinstance(ind, str) and ind.strip():
            return ind.strip()[:255]
    return None


def _real_domain(profile: CompanyProfile, company: Optional[str]) -> Optional[str]:
    # A negative-cache row is keyed by the company name lowercased; that is a
    # sentinel, not a domain (see bulk_enrich_top_companies).
    if not profile.domain or profile.domain == (company or "").strip().lower():
        return None
    return clean_domain(profile.domain)


def fill_lead_from_profile(lead, profile: Optional[CompanyProfile]) -> list:
    """Copy industry, size, description and domain onto `lead` where blank.

    Returns the names of the fields it filled.
    """
    if profile is None:
        return []
    filled = []
    if not lead.industry:
        industry = _first_industry(profile.industries)
        if industry:
            lead.industry = industry
            filled.append("industry")
    if not lead.company_size:
        size = company_size_bucket(profile.employee_count)
        if size:
            lead.company_size = size
            filled.append("company_size")
    if not lead.company_description:
        desc = (profile.short_description or profile.website_summary or "").strip()
        if desc:
            lead.company_description = desc[:_DESCRIPTION_MAX]
            filled.append("company_description")
    if not lead.company_domain:
        domain = _real_domain(profile, lead.company)
        if domain:
            lead.company_domain = domain
            filled.append("company_domain")
    return filled


def cached_profiles_for(db: Session, leads: Iterable) -> Dict[str, CompanyProfile]:
    """Cache lookups for these leads, keyed "d:<domain>" and "n:<lower name>".

    Domain first. A name is only looked up for a lead with no domain, the same
    rule bulk_enrich_top_companies follows, because a name can belong to two
    companies and the domain is the authority when there is one.
    """
    domains, names = set(), set()
    for lead in leads:
        if lead.company_domain:
            domains.add(lead.company_domain.strip().lower())
        elif lead.company:
            names.add(lead.company.strip().lower())

    out: Dict[str, CompanyProfile] = {}
    if domains:
        for p in db.query(CompanyProfile).filter(CompanyProfile.domain.in_(sorted(domains))):
            out["d:" + p.domain] = p
    if names:
        rows = (
            db.query(CompanyProfile)
            .filter(func.lower(CompanyProfile.name).in_(sorted(names)))
            .order_by(CompanyProfile.id)
        )
        for p in rows:
            key = "n:" + (p.name or "").strip().lower()
            # Prefer a row that actually carries data over a negative-cache row.
            has_data = bool(p.industries or p.employee_count or p.short_description)
            if key not in out or (has_data and not out[key].industries and not out[key].employee_count):
                out[key] = p
    return out


def profile_for_lead(lead, profiles: Dict[str, CompanyProfile]) -> Optional[CompanyProfile]:
    if lead.company_domain:
        return profiles.get("d:" + lead.company_domain.strip().lower())
    if lead.company:
        return profiles.get("n:" + lead.company.strip().lower())
    return None


class _RowView:
    """Attribute access over a lead row dict, so rows built for a Core insert
    (lead_collector_service._store_people) go through the same code."""

    def __init__(self, row: dict):
        object.__setattr__(self, "_row", row)

    def __getattr__(self, name):
        return self._row.get(name)

    def __setattr__(self, name, value):
        self._row[name] = value


def _lookup(db: Session, records: list) -> Dict[str, CompanyProfile]:
    """cached_profiles_for, best-effort and inside a savepoint so a failure
    cannot poison the caller's transaction."""
    try:
        with db.begin_nested():
            return cached_profiles_for(db, records)
    except Exception:
        logger.warning("[LEAD_BACKFILL] company cache lookup failed", exc_info=True)
        return {}


def _needs_fill(lead) -> bool:
    return bool(lead.company or lead.company_domain) and not (
        lead.industry and lead.company_size and lead.company_description and lead.company_domain
    )


def cache_for_rows(db: Session, rows: list) -> Dict[str, CompanyProfile]:
    """One lookup for a page of parsed Apollo people (dicts), before insert."""
    views = [_RowView(r) for r in rows if r]
    needy = [v for v in views if _needs_fill(v)]
    return _lookup(db, needy) if needy else {}


def fill_row_from_cache(row: dict, profiles: Dict[str, CompanyProfile]) -> list:
    """fill_lead_from_profile for a row dict about to be inserted."""
    view = _RowView(row)
    return fill_lead_from_profile(view, profile_for_lead(view, profiles))


def backfill_leads_from_cache(db: Session, leads: list) -> int:
    """Fill every lead in `leads` from the cache. One or two queries in all.

    Best-effort: a failed lookup leaves the leads as they were and is logged.
    Returns how many leads gained at least one field.
    """
    needy = [lead for lead in leads if _needs_fill(lead)]
    if not needy:
        return 0
    profiles = _lookup(db, needy)
    touched = 0
    for lead in needy:
        if fill_lead_from_profile(lead, profile_for_lead(lead, profiles)):
            touched += 1
    if touched:
        logger.info("[LEAD_BACKFILL] filled company fields on %d/%d leads from cache",
                    touched, len(needy))
    return touched
