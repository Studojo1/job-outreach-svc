"""Enrichment Routes — Background enrichment jobs with real progress tracking.

Enrichment runs as a background thread (same pattern as test-launch).
Per-lead commits ensure data is never lost. Credits are refunded on failure.
"""

import logging
import threading
import time
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from database.session import get_db, SessionLocal
from database.models import User, Candidate, Lead, OutreachOrder
from api.dependencies import get_current_user
from api.routes_payment import deduct_credits, refund_credits
from core.analytics import capture

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/enrichment", tags=["Enrichment"])

# ── In-memory job tracker (same pattern as _test_launch_jobs) ─────────────────
_enrichment_jobs: dict = {}


class EnrichmentRequest(BaseModel):
    candidate_id: int
    limit: int = 200
    order_id: Optional[int] = None


def _run_enrichment_in_background(
    job_id: str,
    candidate_id: int,
    limit: int,
    user_id: str,
    order_id: Optional[int],
):
    """Background thread: enrich leads one at a time, committing each to DB."""
    from services.enrichment.enrichment_service import _enrich_single_lead

    db = SessionLocal()
    job = _enrichment_jobs[job_id]

    try:
        # Update order status to enriching
        if order_id:
            order = db.query(OutreachOrder).filter_by(id=order_id).first()
            if order and order.status == "leads_ready":
                order.status = "enriching"
                log = list(order.action_log or [])
                log.append({"ts": datetime.utcnow().isoformat(), "msg": f"Enrichment started: {limit} leads"})
                order.action_log = log
                db.commit()

        # Get unenriched leads
        all_unenriched = (
            db.query(Lead)
            .filter(
                Lead.candidate_id == candidate_id,
                (Lead.email.is_(None)) | (Lead.email_verified == False),
            )
            .all()
        )

        if not all_unenriched:
            # All leads already enriched — still update order status
            if order_id:
                order = db.query(OutreachOrder).filter_by(id=order_id).first()
                if order and order.status in ("leads_ready", "enriching"):
                    order.status = "enrichment_complete"
                    log = list(order.action_log or [])
                    log.append({"ts": datetime.utcnow().isoformat(), "msg": "All leads already enriched"})
                    order.action_log = log
                    order.updated_at = datetime.utcnow()
                    db.commit()
            job["status"] = "completed"
            job["progress"] = "No leads found needing enrichment"
            return

        logger.info("[ENRICHMENT_JOB] %s: Pool of %d unenriched leads, target=%d",
                     job_id, len(all_unenriched), limit)

        enriched_count = 0
        failed_count = 0
        idx = 0

        while enriched_count < limit and idx < len(all_unenriched):
            lead = all_unenriched[idx]
            idx += 1

            job["progress"] = f"Enriching lead {enriched_count + failed_count + 1}/{min(limit, len(all_unenriched))}"

            try:
                result = _enrich_single_lead(lead)
                if result:
                    lead.email = result["email"]
                    if result.get("name"):
                        lead.name = result["name"]
                    lead.email_verified = True
                    lead.status = "enriched"
                    db.commit()  # Per-lead commit — data is never lost
                    enriched_count += 1
                    logger.info("[ENRICHMENT_JOB] %s: Enriched %s -> %s", job_id, lead.name, result["email"])
                else:
                    failed_count += 1

                time.sleep(0.2)  # Apollo rate limit

            except Exception as e:
                failed_count += 1
                logger.error("[ENRICHMENT_JOB] %s: Error enriching %s: %s", job_id, lead.name, e)

            # Update job progress
            job["enriched"] = enriched_count
            job["failed"] = failed_count

        # Enrichment complete — refund unused credits
        unused = limit - enriched_count
        if unused > 0 and limit > 5:
            refund_credits(db, user_id, unused)
            db.commit()
            logger.info("[ENRICHMENT_JOB] %s: Refunded %d unused credits", job_id, unused)

        # Update order
        if order_id:
            order = db.query(OutreachOrder).filter_by(id=order_id).first()
            if order:
                order.status = "enrichment_complete"
                order.leads_collected = enriched_count
                log = list(order.action_log or [])
                log.append({"ts": datetime.utcnow().isoformat(),
                            "msg": f"Enrichment complete: {enriched_count} enriched, {failed_count} failed, {unused} credits refunded"})
                order.action_log = log
                order.updated_at = datetime.utcnow()
                db.commit()

        job["status"] = "completed"
        job["enriched"] = enriched_count
        job["failed"] = failed_count
        logger.info("[ENRICHMENT_JOB] %s: Complete — %d enriched, %d failed", job_id, enriched_count, failed_count)
        capture("enrichment_completed", user_id, {
            "job_id": job_id,
            "enriched_count": enriched_count,
            "failed_count": failed_count,
            "credits_refunded": unused if unused > 0 and limit > 5 else 0,
        })

    except Exception as e:
        logger.error("[ENRICHMENT_JOB] %s: Crashed: %s", job_id, e, exc_info=True)
        job["status"] = "failed"
        job["error"] = str(e)

        # Refund all credits on total failure
        if limit > 5:
            try:
                enriched_so_far = job.get("enriched", 0)
                to_refund = limit - enriched_so_far
                if to_refund > 0:
                    refund_credits(db, user_id, to_refund)
                    db.commit()
                    logger.info("[ENRICHMENT_JOB] %s: Refunded %d credits after failure", job_id, to_refund)
            except Exception:
                logger.error("[ENRICHMENT_JOB] %s: Failed to refund credits", job_id, exc_info=True)

        # Update order on failure
        if order_id:
            try:
                order = db.query(OutreachOrder).filter_by(id=order_id).first()
                if order:
                    order.status = "leads_ready"  # Allow retry
                    log = list(order.action_log or [])
                    log.append({"ts": datetime.utcnow().isoformat(), "msg": f"Enrichment failed: {str(e)[:200]}"})
                    order.action_log = log
                    db.commit()
            except Exception:
                pass

    finally:
        db.close()


@router.post("/enrich")
async def enrich_leads(
    request: EnrichmentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Start enrichment as a background job. Returns job_id for polling.

    Credits are deducted upfront and refunded for any un-enriched leads.
    """
    candidate = db.query(Candidate).filter_by(
        id=request.candidate_id, user_id=current_user.id
    ).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    # Check there are leads to enrich
    unenriched_count = db.query(Lead).filter(
        Lead.candidate_id == request.candidate_id,
        (Lead.email.is_(None)) | (Lead.email_verified == False),
    ).count()
    if unenriched_count == 0:
        raise HTTPException(status_code=400, detail="No leads found needing enrichment")

    # Credit check (free tier bypasses)
    if request.limit > 5:
        if not deduct_credits(db, current_user.id, request.limit):
            raise HTTPException(
                status_code=402,
                detail="Insufficient credits. Please purchase an enrichment package first.",
            )
        db.commit()  # Commit credit deduction immediately

    # Create background job
    job_id = str(uuid.uuid4())[:8]
    _enrichment_jobs[job_id] = {
        "status": "processing",
        "progress": "Starting enrichment...",
        "enriched": 0,
        "failed": 0,
        "total": min(request.limit, unenriched_count),
        "error": "",
        "started_at": datetime.utcnow().isoformat(),
    }

    thread = threading.Thread(
        target=_run_enrichment_in_background,
        args=(job_id, request.candidate_id, request.limit, str(current_user.id), request.order_id),
        daemon=True,
    )
    thread.start()

    logger.info("[ENRICHMENT] Job %s started for user %s, limit=%d", job_id, current_user.id, request.limit)
    capture("enrichment_started", str(current_user.id), {
        "job_id": job_id,
        "candidate_id": request.candidate_id,
        "lead_limit": request.limit,
        "unenriched_available": unenriched_count,
    })

    return {
        "status": "processing",
        "job_id": job_id,
        "total": min(request.limit, unenriched_count),
    }


@router.get("/{job_id}/status")
async def enrichment_status(job_id: str):
    """Poll for enrichment job progress."""
    job = _enrichment_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return {
        "job_id": job_id,
        "status": job["status"],
        "progress": job.get("progress", ""),
        "enriched": job.get("enriched", 0),
        "failed": job.get("failed", 0),
        "total": job.get("total", 0),
        "error": job.get("error", ""),
        "started_at": job.get("started_at", ""),
    }
