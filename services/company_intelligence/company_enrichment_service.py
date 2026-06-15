"""Company enrichment orchestrator.

Coordinates LLM web-search company research + light website scrape, persists
into the global `company_profiles` cache, and serves enriched payloads to the
lead-justifier downstream.

Cache rules:
- LLM research: refreshed if `last_apollo_enriched_at` is older than 30 days.
  (Column reused — no migration needed.)
- Name-based lookup: when a lead has no company_domain, we look up by
  normalised company name first so the 30-day cache actually hits.
- Site scrape: refreshed if `last_scraped_at` is older than 30 days AND
  `scrape_failed` is False (we never retry permanently broken sites).
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from database.models import CompanyProfile

from .llm_company_research import research_companies_bulk
from .apollo_company_resolver import resolve_canonical_domain

from .web_scraper import scrape_many

logger = logging.getLogger(__name__)

CACHE_TTL = timedelta(days=90)


def get_or_create_profile(db: Session, domain: str) -> CompanyProfile:
    profile = db.query(CompanyProfile).filter_by(domain=domain).first()
    if profile is None:
        profile = CompanyProfile(domain=domain)
        db.add(profile)
        db.flush()
    return profile


def _apollo_stale(profile: CompanyProfile) -> bool:
    if not profile.last_apollo_enriched_at:
        return True
    return profile.last_apollo_enriched_at < datetime.utcnow() - CACHE_TTL


def _scrape_stale(profile: CompanyProfile) -> bool:
    if profile.scrape_failed:
        return False
    if not profile.last_scraped_at:
        return True
    return profile.last_scraped_at < datetime.utcnow() - CACHE_TTL


def _apply_apollo_payload(profile: CompanyProfile, payload: dict) -> None:
    profile.apollo_org_id = payload.get("apollo_org_id") or profile.apollo_org_id
    profile.name = payload.get("name") or profile.name
    profile.short_description = payload.get("short_description") or profile.short_description
    if payload.get("industries"):
        profile.industries = payload["industries"]
    if payload.get("keywords"):
        profile.keywords = payload["keywords"]
    if payload.get("technologies"):
        profile.technologies = payload["technologies"]
    if payload.get("employee_count") is not None:
        profile.employee_count = payload["employee_count"]
    if payload.get("founded_year") is not None:
        profile.founded_year = payload["founded_year"]
    if payload.get("headquarters_city"):
        profile.headquarters_city = payload["headquarters_city"]
    # Round-2 momentum signals
    if payload.get("recent_news_summary"):
        profile.recent_news_summary = payload["recent_news_summary"]
    if payload.get("latest_funding_round_date"):
        try:
            profile.latest_funding_round_date = datetime.fromisoformat(payload["latest_funding_round_date"])
        except (ValueError, TypeError):
            pass
    if payload.get("latest_funding_amount") is not None:
        profile.latest_funding_amount = payload["latest_funding_amount"]
    if payload.get("linkedin_url"):
        profile.linkedin_url = payload["linkedin_url"]
    profile.last_apollo_enriched_at = datetime.utcnow()


def _apply_llm_research(profile: CompanyProfile, facts: dict) -> None:
    """Persist LLM web-search facts onto a CompanyProfile row.

    `facts` is the dict returned by llm_company_research.research_company().
    It contains both extracted_facts fields AND company-level fields.
    We write both so the justifier and lead scorer get fresh data.
    """
    profile.name = facts.get("name") or profile.name
    if facts.get("industries"):
        profile.industries = facts["industries"]
    if facts.get("keywords"):
        profile.keywords = facts["keywords"]
    if facts.get("employee_count") is not None:
        profile.employee_count = facts["employee_count"]

    # Store the structured facts blob (what_they_build, core_tech, etc.)
    # directly — no separate extractor step needed.
    structured = {
        k: facts[k]
        for k in ("what_they_build", "core_tech", "primary_market",
                  "stage_signal", "recent_momentum", "hiring_signal")
        if k in facts
    }
    if any(v for v in structured.values()):
        profile.extracted_facts = structured

    profile.last_apollo_enriched_at = datetime.utcnow()


def _apply_scrape_payload(profile: CompanyProfile, payload: Optional[dict]) -> None:
    profile.last_scraped_at = datetime.utcnow()
    if payload is None:
        profile.scrape_failed = True
        return
    profile.scrape_failed = False
    if payload.get("meta_title"):
        profile.scrape_meta_title = payload["meta_title"]
    if payload.get("meta_description"):
        profile.scrape_meta_description = payload["meta_description"]
    if payload.get("hero_text"):
        profile.scrape_hero_text = payload["hero_text"]
    if payload.get("summary"):
        profile.website_summary = payload["summary"]
    # Multi-page sections (added in migration 027). Each is None unless the
    # scraper found content for that section.
    if payload.get("product_summary"):
        profile.product_page_summary = payload["product_summary"]
    if payload.get("blog_summary"):
        profile.blog_summary = payload["blog_summary"]
    if payload.get("careers_summary"):
        profile.careers_summary = payload["careers_summary"]


def bulk_enrich_top_companies(
    db: Session,
    companies: Iterable[Any],
    enable_scrape: bool = True,
    enable_llm_research: bool = True,
    location_hint: Optional[List[str]] = None,
) -> Dict[str, CompanyProfile]:
    """Enrich every company in the input via cache + optional LLM web search.

    Accepts two input shapes (back-compatible):
      - List of domain strings (legacy)
      - List of {"domain": str|None, "name": str|None} dicts (new)

    Cache strategy:
      0. Resolve canonical domain via Apollo /mixed_companies/search (free)
         when we have a name. Uses `location_hint` to disambiguate common
         short names (Comet, Swish, ...) to the right org. Runs only when
         we'd otherwise do fresh research (enable_llm_research=True).
      1. If domain is known → check CompanyProfile by domain (30-day TTL)
      2. If only name is known → check CompanyProfile by normalised name field
      3. Cache miss → call LLM web search only if enable_llm_research=True
      4. Persist by resolved domain; register name alias for caller lookup

    Set enable_llm_research=False and enable_scrape=False for cache-only mode
    (scoring pipeline uses this to stay fast; web research runs separately).

    Args:
        location_hint: Optional list of locations (e.g. ["Bangalore", "India"])
            inherited from the user's lead filter. Passed to Apollo company
            search so ambiguous names resolve to the org actually in scope.

    Returns {key: CompanyProfile} where key is domain OR original name.
    """
    norm_companies: List[Dict[str, Optional[str]]] = []
    for entry in companies:
        if isinstance(entry, str):
            norm_companies.append({"domain": entry.strip().lower() if entry else None, "name": None})
        elif isinstance(entry, dict):
            d = (entry.get("domain") or "").strip().lower() or None
            n = (entry.get("name") or "").strip() or None
            if d or n:
                norm_companies.append({"domain": d, "name": n})

    if not norm_companies:
        return {}

    seen: set[str] = set()
    deduped: List[Dict[str, Optional[str]]] = []
    for c in norm_companies:
        key = (c["domain"] or c["name"] or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(c)

    logger.info("[ENRICH] starting bulk enrich for %d unique companies", len(deduped))

    # ── Phase 0: resolve canonical domain via Apollo /mixed_companies/search ─
    # FREE Apollo call. Disambiguates ambiguous short names (Comet, Swish, …)
    # to the right org by combining the user's location filter with the org
    # name. Whatever domain Apollo's free people search left us with (often
    # wrong because the people endpoint doesn't actually return a domain) gets
    # overridden by the canonical primary_domain from the search hit.
    # Only run when we'd otherwise do fresh research — cache-only callers
    # skip this to stay fast.
    if enable_llm_research:
        resolved_count = 0
        for c in deduped:
            if not c.get("name"):
                continue
            resolved = resolve_canonical_domain(c["name"], location_hint=location_hint)
            if not resolved:
                continue
            new_domain = resolved.get("primary_domain")
            if not new_domain:
                continue
            old_domain = c.get("domain")
            if old_domain and old_domain != new_domain:
                logger.info(
                    "[ENRICH] corrected domain for %r: %s → %s (Apollo search disambiguation)",
                    c["name"], old_domain, new_domain,
                )
            c["domain"] = new_domain
            resolved_count += 1
        logger.info("[ENRICH] phase 0: resolved canonical domain for %d/%d companies via Apollo search",
                    resolved_count, len(deduped))

    profiles: Dict[str, CompanyProfile] = {}   # keyed by canonical domain
    name_alias: Dict[str, CompanyProfile] = {} # original name → profile
    to_research: Dict[str, Optional[str]] = {} # name → domain for LLM calls

    # ── Phase 1: cache check (domain-first, then name-based fallback) ────────
    for c in deduped:
        domain = c["domain"]
        name = c["name"]

        # Try domain-based cache hit first
        if domain:
            existing = db.query(CompanyProfile).filter_by(domain=domain).first()
            if existing and not _apollo_stale(existing):
                profiles[domain] = existing
                if name:
                    name_alias[name] = existing
                continue

        # Name-based fallback — ONLY when we have no domain at all. If Phase 0
        # gave us a canonical domain (or one was already on the lead), the
        # domain is authoritative; name-based cache lookup would re-use a stale
        # CompanyProfile for a same-named-but-different company (e.g. cache
        # for 'Swish'@swish.nu hijacking a fresh resolver hit on justswish.in).
        if name and not domain:
            existing = (
                db.query(CompanyProfile)
                .filter(CompanyProfile.name.ilike(name))
                .first()
            )
            if existing and not _apollo_stale(existing):
                key = existing.domain or name.lower()
                profiles[key] = existing
                name_alias[name] = existing
                continue

        # Cache miss — queue for LLM research
        to_research[name or domain] = domain

    logger.info(
        "[ENRICH] cache: %d hits, %d need LLM research",
        len(profiles), len(to_research),
    )

    # ── Phase 2: LLM web search for cache misses (parallel) ─────────────────
    if to_research and enable_llm_research:
        research_results = research_companies_bulk(to_research)
        llm_ok = 0
        for input_key, facts in research_results.items():
            if not facts:
                continue
            llm_ok += 1
            resolved_domain = facts.get("discovered_domain") or input_key.lower()
            p = get_or_create_profile(db, resolved_domain)
            _apply_llm_research(p, facts)
            profiles[resolved_domain] = p
            # Register name alias so callers can find by original company name
            name = facts.get("name") or input_key
            if name and name.lower() != resolved_domain:
                name_alias[name] = p
        logger.info("[ENRICH] llm_research: %d/%d succeeded", llm_ok, len(to_research))

    unique_domains = sorted(profiles.keys())

    # ── Phase 3: Apollo job postings — DISABLED (Apollo org calls cost credits)
    # LLM web search already extracts hiring_signal from live web pages.

    # ── Phase 4: Website scrape (async, parallel) ────────────────────────────
    if enable_scrape:
        scrape_to_run = [d for d in unique_domains if _scrape_stale(profiles[d])]
        logger.info("[ENRICH] scrape: %d need refresh out of %d", len(scrape_to_run), len(unique_domains))
        if scrape_to_run:
            try:
                scrape_results = asyncio.run(scrape_many(scrape_to_run, concurrency=10))
            except RuntimeError:
                loop = asyncio.new_event_loop()
                try:
                    scrape_results = loop.run_until_complete(scrape_many(scrape_to_run, concurrency=10))
                finally:
                    loop.close()
            scrape_ok = 0
            for d, payload in scrape_results.items():
                _apply_scrape_payload(profiles[d], payload)
                if payload is not None:
                    scrape_ok += 1
            logger.info("[ENRICH] scrape: %d/%d succeeded", scrape_ok, len(scrape_to_run))

    db.commit()
    out: Dict[str, CompanyProfile] = dict(profiles)
    for nm, p in name_alias.items():
        if nm not in out:
            out[nm] = p
    return out


def profile_to_extractor_dict(profile: CompanyProfile) -> dict:
    """Wider payload passed to the fact-extractor LLM. Includes everything
    we have — multi-page scrape sections, news, job postings — because the
    extractor's job is to distill all of it into the structured facts blob."""
    return {
        "name": profile.name,
        "domain": profile.domain,
        "short_description": (profile.short_description or "")[:1500] or None,
        "industries": (profile.industries or [])[:8] or None,
        "keywords": (profile.keywords or [])[:15] or None,
        "technologies": (profile.technologies or [])[:15] or None,
        "employee_count": profile.employee_count,
        "founded_year": profile.founded_year,
        "headquarters_city": profile.headquarters_city,
        "recent_news_summary": (profile.recent_news_summary or "")[:600] or None,
        "latest_funding_amount": profile.latest_funding_amount,
        "website_summary": (profile.website_summary or "")[:1500] or None,
        "product_page_summary": (profile.product_page_summary or "")[:800] or None,
        "blog_summary": (profile.blog_summary or "")[:800] or None,
        "careers_summary": (profile.careers_summary or "")[:800] or None,
        "recent_job_postings": profile.recent_job_postings or [],
    }


def profile_to_llm_dict(profile: CompanyProfile) -> dict:
    """Compact representation passed to the justifier LLM. Now leans on the
    structured `extracted_facts` blob so the justifier reasons over distilled
    facts instead of raw text dumps. Falls back to raw fields if extraction
    hasn't run yet (cache miss / new company)."""
    return {
        "name": profile.name,
        "domain": profile.domain,
        # The most important new field: structured facts from the extractor.
        # If null, the justifier falls back to the raw fields below.
        "facts": profile.extracted_facts or None,
        "short_description": (profile.short_description or "")[:600] or None,
        "industries": (profile.industries or [])[:6] or None,
        "keywords": (profile.keywords or [])[:10] or None,
        "technologies": (profile.technologies or [])[:10] or None,
        "employee_count": profile.employee_count,
        "founded_year": profile.founded_year,
        "headquarters_city": profile.headquarters_city,
        "website_summary": (profile.website_summary or "")[:1000] or None,
        # Round-2 raw signals — passed for the rare case where facts is None
        # AND the justifier needs to fall back to raw text.
        "recent_news_summary": (profile.recent_news_summary or "")[:400] or None,
        "recent_job_postings": (profile.recent_job_postings or [])[:3] or None,
    }
