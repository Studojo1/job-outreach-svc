"""Company enrichment orchestrator.

Coordinates Apollo /organizations/enrich + light website scrape, persists into
the global `company_profiles` cache, and serves enriched payloads to the
lead-justifier downstream.

Cache rules:
- Apollo enrich: refreshed if `last_apollo_enriched_at` is older than 30 days.
- Site scrape: refreshed if `last_scraped_at` is older than 30 days AND
  `scrape_failed` is False (we never retry permanently broken sites).
- Domains we've already scraped failures for stay marked failed forever.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from database.models import CompanyProfile

from .apollo_org_enrich import enrich_organization
from .apollo_job_postings import fetch_job_postings
from .company_fact_extractor import extract_company_facts
from .web_scraper import scrape_many

logger = logging.getLogger(__name__)

CACHE_TTL = timedelta(days=30)


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
    domains: Iterable[str],
    enable_scrape: bool = True,
) -> Dict[str, CompanyProfile]:
    """Enrich every domain in `domains` (deduplicated, falsy-stripped).

    Returns a {domain: CompanyProfile} dict for every domain processed
    (including ones whose Apollo + scrape both failed — caller can still read
    whatever partial data was previously cached).
    """
    unique_domains: List[str] = sorted({d.strip().lower() for d in domains if d and d.strip()})
    if not unique_domains:
        return {}

    logger.info("[ENRICH] starting bulk enrich for %d unique domains", len(unique_domains))

    # ── Apollo enrich (sync, sequential — already throttled at 0.2s/req)
    apollo_to_run: List[str] = []
    profiles: Dict[str, CompanyProfile] = {}
    for d in unique_domains:
        p = get_or_create_profile(db, d)
        profiles[d] = p
        if _apollo_stale(p):
            apollo_to_run.append(d)

    logger.info("[ENRICH] apollo: %d need refresh out of %d", len(apollo_to_run), len(unique_domains))
    apollo_ok = 0
    for d in apollo_to_run:
        payload = enrich_organization(d)
        if payload:
            _apply_apollo_payload(profiles[d], payload)
            apollo_ok += 1
        else:
            # Stamp the timestamp so we don't hammer a domain that returns nothing.
            profiles[d].last_apollo_enriched_at = datetime.utcnow()
    logger.info("[ENRICH] apollo: %d/%d succeeded", apollo_ok, len(apollo_to_run))

    # ── Apollo job postings (per company that has an apollo_org_id).
    # Run sequentially since they're already cheap (single HTTP call) and
    # share the 0.2s throttle inside fetch_job_postings.
    jobs_to_run = [d for d in unique_domains if profiles[d].apollo_org_id and (
        profiles[d].recent_job_postings is None or _apollo_stale(profiles[d])
    )]
    if jobs_to_run:
        logger.info("[ENRICH] jobs: %d companies need posting refresh", len(jobs_to_run))
        jobs_ok = 0
        for d in jobs_to_run:
            postings = fetch_job_postings(profiles[d].apollo_org_id)
            # Always store (even empty list) so we don't keep retrying companies
            # that genuinely have no public listings.
            profiles[d].recent_job_postings = postings
            if postings:
                jobs_ok += 1
        logger.info("[ENRICH] jobs: %d/%d had postings", jobs_ok, len(jobs_to_run))

    # ── Website scrape (async, parallel)
    if enable_scrape:
        scrape_to_run = [d for d in unique_domains if _scrape_stale(profiles[d])]
        logger.info("[ENRICH] scrape: %d need refresh out of %d", len(scrape_to_run), len(unique_domains))
        if scrape_to_run:
            try:
                results = asyncio.run(scrape_many(scrape_to_run, concurrency=10))
            except RuntimeError:
                # asyncio.run inside an existing loop — should not happen since
                # the caller invokes this via asyncio.to_thread, but guard anyway.
                loop = asyncio.new_event_loop()
                try:
                    results = loop.run_until_complete(scrape_many(scrape_to_run, concurrency=10))
                finally:
                    loop.close()
            scrape_ok = 0
            for d, payload in results.items():
                _apply_scrape_payload(profiles[d], payload)
                if payload is not None:
                    scrape_ok += 1
            logger.info("[ENRICH] scrape: %d/%d succeeded", scrape_ok, len(scrape_to_run))

    # ── Per-company fact extraction (LLM). Runs LAST so it can reason over
    # the freshly-populated Apollo + scrape + jobs data. We refresh whenever
    # extracted_facts is missing OR when Apollo data was just refreshed.
    facts_to_run = [
        d for d in unique_domains
        if profiles[d].extracted_facts is None or _apollo_stale(profiles[d])
    ]
    if facts_to_run:
        logger.info("[ENRICH] facts: extracting for %d companies", len(facts_to_run))
        extractor_input = {
            d: profile_to_extractor_dict(profiles[d]) for d in facts_to_run
        }
        facts_by_domain = extract_company_facts(extractor_input)
        for d, facts in facts_by_domain.items():
            if facts:
                profiles[d].extracted_facts = facts

    db.commit()
    return profiles


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
