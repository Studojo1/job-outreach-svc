"""Enrichment Service — Enrich leads with verified emails via Apollo.

Uses Apollo's People Match API to find verified email addresses
for discovered leads. Supports partial enrichment with a user-controlled
limit to manage API costs.
"""

import time
from dataclasses import dataclass
from typing import Dict, Any, List, Optional

from sqlalchemy.orm import Session

from database.models import Lead
from core.logger import get_logger
from services.shared.apollo_key_manager import apollo_post, apollo_keys

logger = get_logger(__name__)

APOLLO_MATCH_URL = "https://api.apollo.io/api/v1/people/match"


def _record_apollo_reveal() -> None:
    """Increment today's (IST) Apollo reveal counter — 1 credit per reveal.

    Best-effort and self-contained: opens its own short-lived session and never
    raises, so a counter hiccup can never break enrichment. The emailer-service
    reads apollo_usage_daily and pages ops if a day's reveals cross the threshold.
    """
    try:
        from database.session import SessionLocal
        from sqlalchemy import text
        db = SessionLocal()
        try:
            db.execute(text(
                "INSERT INTO apollo_usage_daily (day, reveals) "
                "VALUES ((now() AT TIME ZONE 'Asia/Kolkata')::date, 1) "
                "ON CONFLICT (day) DO UPDATE SET reveals = apollo_usage_daily.reveals + 1"
            ))
            db.commit()
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 — counter is non-essential
        logger.warning("[ENRICHMENT] apollo reveal counter failed: %s", e)


# Classified result from a single enrichment attempt. Lets the JIT worker
# distinguish "Apollo cannot find this person" (permanent for this lead) from
# "Apollo credits exhausted / rate-limited / API down" (retry when recovered).
@dataclass
class EnrichmentResult:
    success: bool
    data: Optional[Dict[str, str]] = None
    # one of: "no_match" | "credit_exhausted" | "rate_limited" | "apollo_down" | "exception"
    error_type: Optional[str] = None
    error_detail: str = ""


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


def enrich_single_lead_classified(lead: Lead) -> EnrichmentResult:
    """Call Apollo People Match for a single lead, returning a classified result.

    Distinguishes:
      - success (email found)
      - no_match (Apollo responded but no email available — permanent for this lead)
      - credit_exhausted (Apollo credits depleted — recoverable when topped up)
      - rate_limited (transient — retry next cycle)
      - apollo_down (5xx — transient)
      - exception (other unexpected error)
    """
    if not apollo_keys.has_valid_key():
        logger.error("[ENRICHMENT] No valid Apollo API key available")
        return EnrichmentResult(success=False, error_type="credit_exhausted",
                                error_detail="No valid Apollo key in pool")

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

    try:
        resp = apollo_post(APOLLO_MATCH_URL, json=payload, timeout=15)
    except RuntimeError as e:
        # apollo_key_manager raises this when all keys are exhausted
        msg = str(e)
        logger.warning("[ENRICHMENT] Apollo keys exhausted: %s", msg[:200])
        return EnrichmentResult(success=False, error_type="credit_exhausted", error_detail=msg)
    except Exception as e:
        logger.error("[ENRICHMENT] Apollo call raised for %s: %s", lead.name, e)
        return EnrichmentResult(success=False, error_type="exception", error_detail=str(e)[:200])

    status = resp.status_code
    if status in (402, 403):
        return EnrichmentResult(success=False, error_type="credit_exhausted",
                                error_detail=f"HTTP {status}")
    if status == 429:
        return EnrichmentResult(success=False, error_type="rate_limited", error_detail="HTTP 429")
    if 500 <= status < 600:
        return EnrichmentResult(success=False, error_type="apollo_down",
                                error_detail=f"HTTP {status}")
    if not resp.ok:
        # Other 4xx — treat as no_match (Apollo won't find this person on retry)
        logger.warning("[ENRICHMENT] Apollo match failed for %s: HTTP %d", lead.name, status)
        return EnrichmentResult(success=False, error_type="no_match",
                                error_detail=f"HTTP {status}")

    data = resp.json()

    # Apollo returns HTTP 200 but embeds a credit/access error in the body.
    body_error = data.get("error") or ""
    if body_error and any(phrase in body_error.lower() for phrase in (
        "insufficient credits", "not accessible", "upgrade your plan", "credit limit",
    )):
        logger.warning("[ENRICHMENT] Apollo key exhausted (body error): %s", body_error[:120])
        current_key = apollo_keys.get_key()
        if current_key:
            apollo_keys.report_failure(current_key, 402)
        return EnrichmentResult(success=False, error_type="credit_exhausted",
                                error_detail=body_error[:200])

    person = data.get("person", {})
    email = person.get("email")
    if not email:
        return EnrichmentResult(success=False, error_type="no_match",
                                error_detail="Apollo returned no email")

    # Only trust emails Apollo has actually VERIFIED. Apollo also returns
    # "guessed"/"extrapolated"/"unavailable" pattern-based addresses that bounce
    # hard on protected domains (Proofpoint, academic, defence), which wrecks the
    # sender's reputation. Treat anything not verified as a no-match so the lead
    # is skipped (and can be replaced) rather than emailed into a bounce.
    email_status = (person.get("email_status") or "").lower()
    if email_status and email_status != "verified":
        return EnrichmentResult(success=False, error_type="no_match",
                                error_detail=f"Apollo email not verified (status={email_status})")

    result_dict: Dict[str, str] = {"email": email}
    first = person.get("first_name") or ""
    last = person.get("last_name") or ""
    if not last:
        obfuscated = person.get("last_name_obfuscated") or ""
        if "*" not in obfuscated:
            last = obfuscated
    full_name = f"{first} {last}".strip()
    if full_name:
        result_dict["name"] = full_name
    _record_apollo_reveal()  # 1 successful reveal = 1 Apollo credit burned
    return EnrichmentResult(success=True, data=result_dict)


def _enrich_single_lead(lead: Lead) -> Optional[Dict[str, str]]:
    """Backward-compatible wrapper. Returns dict on success, None on any failure.

    Callers that need to distinguish credit-exhausted from no-match should use
    enrich_single_lead_classified() instead.
    """
    result = enrich_single_lead_classified(lead)
    if result.success:
        return result.data
    # Preserve historical contract: credit-exhausted bubbles up as exception so
    # batch callers can fail-fast and surface the issue to the user.
    if result.error_type == "credit_exhausted":
        raise RuntimeError(f"Apollo key exhausted: {result.error_detail}")
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

