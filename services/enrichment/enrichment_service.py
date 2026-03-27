"""Enrichment Service — Enrich leads with verified emails via Apollo.

Uses Apollo's People Match API to find verified email addresses
for discovered leads. Supports partial enrichment with a user-controlled
limit to manage API costs.
"""

import time
from typing import Dict, Any, List, Optional

import requests
from sqlalchemy.orm import Session

from core.config import settings
from database.models import Lead
from core.logger import get_logger

logger = get_logger(__name__)

APOLLO_MATCH_URL = "https://api.apollo.io/api/v1/people/match"


def enrich_contacts(
    db: Session,
    candidate_id: int,
    limit: int = 20,
) -> Dict[str, Any]:
    """Enrich leads for a candidate with verified emails.

    Implements backfill: if enrichment fails for any lead, automatically
    pulls the next unenriched lead from the pool and retries until the
    requested count is met or the pool is exhausted.
    """
    logger.info("[ENRICHMENT] Starting enrichment for candidate_id=%d, limit=%d", candidate_id, limit)

    all_unenriched = (
        db.query(Lead)
        .filter(
            Lead.candidate_id == candidate_id,
            (Lead.email.is_(None)) | (Lead.email_verified == False),
        )
        .all()
    )

    if not all_unenriched:
        logger.info("[ENRICHMENT] No leads found needing enrichment")
        return {"total": 0, "enriched": 0, "failed": 0, "contacts": []}

    logger.info("[ENRICHMENT] Pool of %d unenriched leads, target=%d", len(all_unenriched), limit)

    enriched_count = 0
    failed_count = 0
    enriched_contacts: List[Dict[str, Any]] = []
    attempted_ids: set = set()

    idx = 0
    while enriched_count < limit and idx < len(all_unenriched):
        lead = all_unenriched[idx]
        idx += 1

        if lead.id in attempted_ids:
            continue
        attempted_ids.add(lead.id)

        try:
            result = _enrich_single_lead(lead)
            if result:
                lead.email = result["email"]
                if result.get("name"):
                    lead.name = result["name"]
                lead.email_verified = True
                lead.status = "enriched"
                enriched_count += 1
                enriched_contacts.append({
                    "id": lead.id,
                    "name": lead.name,
                    "title": lead.title,
                    "company": lead.company,
                    "industry": lead.industry,
                    "email": result["email"],
                    "linkedin_url": lead.linkedin_url,
                })
                logger.info("[ENRICHMENT] Enriched: %s -> %s", lead.name, result["email"])
            else:
                failed_count += 1
                logger.warning("[ENRICHMENT] No email found for: %s (backfilling)", lead.name)

            time.sleep(0.2)

        except Exception as e:
            failed_count += 1
            logger.error("[ENRICHMENT] Error enriching %s: %s", lead.name, e)

    db.commit()

    logger.info(
        "[ENRICHMENT] Complete: %d enriched, %d failed, %d attempted",
        enriched_count, failed_count, len(attempted_ids),
    )

    return {
        "total": len(attempted_ids),
        "enriched": enriched_count,
        "failed": failed_count,
        "contacts": enriched_contacts,
    }


def _enrich_single_lead(lead: Lead) -> Optional[Dict[str, str]]:
    """Call Apollo People Match for a single lead.

    Returns:
        Dict with 'email' key if found, None otherwise.
    """
    if not settings.APOLLO_API_KEY or settings.APOLLO_API_KEY == "your_apollo_key_here":
        logger.error("[ENRICHMENT] APOLLO_API_KEY not configured")
        return None

    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": settings.APOLLO_API_KEY,
    }

    payload: Dict[str, Any] = {}

    if lead.name:
        parts = lead.name.split(" ", 1)
        payload["first_name"] = parts[0]
        if len(parts) > 1:
            payload["last_name"] = parts[1]

    if lead.company:
        payload["organization_name"] = lead.company

    if lead.title:
        payload["title"] = lead.title

    if lead.linkedin_url:
        payload["linkedin_url"] = lead.linkedin_url

    if lead.apollo_id:
        payload["id"] = lead.apollo_id

    resp = requests.post(APOLLO_MATCH_URL, json=payload, headers=headers, timeout=15)

    if not resp.ok:
        logger.warning("[ENRICHMENT] Apollo match failed for %s: %d", lead.name, resp.status_code)
        return None

    data = resp.json()
    person = data.get("person", {})

    email = person.get("email")
    if email:
        result_dict = {"email": email}

        first = person.get("first_name") or ""
        last = person.get("last_name") or ""
        if not last:
            obfuscated = person.get("last_name_obfuscated") or ""
            if "*" not in obfuscated:
                last = obfuscated

        full_name = f"{first} {last}".strip()
        if full_name:
            result_dict["name"] = full_name

        return result_dict

    return None

def enrich_preview_leads(candidate_id: int, n: int = 15) -> None:
    """Immediately enrich the first N unenriched leads for a candidate.

    Called in the background when a user enters campaign setup, so that
    preview emails and test emails have enriched leads to work with.
    Credits are NOT deducted here — they are deducted by the JIT worker
    when it processes each lead 3 hours before the scheduled send time.

    Args:
        candidate_id: The candidate whose leads to enrich.
        n: Number of leads to enrich (default 15 — enough for 5 test emails + buffer).
    """
    from database.session import SessionLocal
    from database.models import LeadScore

    db = SessionLocal()
    try:
        leads = (
            db.query(Lead)
            .filter(
                Lead.candidate_id == candidate_id,
                Lead.email.is_(None),
            )
            .outerjoin(LeadScore, LeadScore.lead_id == Lead.id)
            .order_by(LeadScore.overall_score.desc().nullslast(), Lead.id.asc())
            .limit(n)
            .all()
        )

        if not leads:
            logger.info("[PREVIEW-ENRICH] No unenriched leads for candidate %d", candidate_id)
            return

        logger.info("[PREVIEW-ENRICH] Enriching %d leads for candidate %d", len(leads), candidate_id)
        enriched = 0

        for lead in leads:
            try:
                result = _enrich_single_lead(lead)
                if result:
                    lead.email = result["email"]
                    if result.get("name"):
                        lead.name = result["name"]
                    lead.email_verified = True
                    lead.status = "enriched"
                    db.commit()
                    enriched += 1
                    logger.info("[PREVIEW-ENRICH] Enriched lead %d (%s) -> %s",
                                lead.id, lead.name, result["email"])
            except Exception as e:
                logger.warning("[PREVIEW-ENRICH] Failed to enrich lead %d: %s", lead.id, e)

            time.sleep(0.2)  # Apollo rate limit

        logger.info("[PREVIEW-ENRICH] Done: %d/%d enriched for candidate %d",
                    enriched, len(leads), candidate_id)
    except Exception as e:
        logger.error("[PREVIEW-ENRICH] Error in preview enrichment for candidate %d: %s",
                     candidate_id, e)
    finally:
        db.close()

