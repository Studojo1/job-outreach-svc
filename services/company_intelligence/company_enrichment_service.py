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

    db.commit()
    return profiles


def profile_to_llm_dict(profile: CompanyProfile) -> dict:
    """Compact representation passed to the justifier LLM. Trim long fields
    to keep batched-prompt token cost bounded."""
    return {
        "name": profile.name,
        "domain": profile.domain,
        "short_description": (profile.short_description or "")[:600] or None,
        "industries": (profile.industries or [])[:6] or None,
        "keywords": (profile.keywords or [])[:10] or None,
        "technologies": (profile.technologies or [])[:10] or None,
        "employee_count": profile.employee_count,
        "founded_year": profile.founded_year,
        "headquarters_city": profile.headquarters_city,
        "website_summary": (profile.website_summary or "")[:1000] or None,
    }
