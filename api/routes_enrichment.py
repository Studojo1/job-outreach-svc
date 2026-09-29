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
from core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/enrichment", tags=["Enrichment"])

# ── Job tracker, persisted (audit P31) ───────────────────────────────────────
# Jobs lived in a module-level dict: another replica could not answer a status
# poll, and a restart lost the job together with the credits it had reserved.
# Every assignment to a job's fields now writes through to enrichment_jobs, and
# services/reconcile.py releases what a job that died mid-run still holds.
_JOB_FIELDS = {"status", "progress", "enriched", "failed", "total", "error", "reserved", "released"}


class _PersistentJob(dict):
    def __init__(self, job_id: str, data: dict):
        super().__init__(data)
        self.job_id = job_id

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if key in _JOB_FIELDS:
            _write_job(self.job_id, {key: value})


def _write_job(job_id: str, fields: dict) -> None:
    from database.models import EnrichmentJob
    s = SessionLocal()
    try:
        row = s.get(EnrichmentJob, job_id)
        if row is not None:
            for k, v in fields.items():
                setattr(row, k, v if k != "error" else (str(v)[:2000] if v else v))
            row.updated_at = datetime.utcnow()
            s.commit()
    except Exception:
        s.rollback()
        logger.exception("[ENRICHMENT_JOB] could not persist %s", job_id)
    finally:
        s.close()


class _JobStore:
    """dict-like: jobs[job_id] -> a job whose field writes persist."""

    def __setitem__(self, job_id: str, data: dict) -> None:
        from database.models import EnrichmentJob
        s = SessionLocal()
        try:
            s.merge(EnrichmentJob(
                id=job_id, user_id=data["user_id"], status=data.get("status", "processing"),
                reserved=data.get("reserved", 0), released=data.get("released", 0),
                total=data.get("total", 0), enriched=data.get("enriched", 0), failed=data.get("failed", 0),
                progress=data.get("progress"), error=data.get("error") or None,
            ))
            s.commit()
        finally:
            s.close()

    def get(self, job_id: str):
        from database.models import EnrichmentJob
        s = SessionLocal()
        try:
            row = s.get(EnrichmentJob, job_id)
            if row is None:
                return None
            return _PersistentJob(job_id, {
                "status": row.status, "progress": row.progress or "", "enriched": row.enriched,
                "failed": row.failed, "total": row.total, "error": row.error or "",
                "reserved": row.reserved, "released": row.released, "user_id": row.user_id,
                "started_at": row.created_at.isoformat() if row.created_at else "",
            })
        finally:
            s.close()

    def __getitem__(self, job_id: str):
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job


_enrichment_jobs = _JobStore()


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
    """Background thread: enrich leads one at a time, committing each to DB.

    order_id comes straight from the request body, so every order lookup here
    is scoped to user_id: a caller must not be able to move someone else's order.
    """
    from services.enrichment.enrichment_service import _enrich_single_lead

    db = SessionLocal()
    job = _enrichment_jobs[job_id]

    try:
        # Update order status to enriching
        if order_id:
            order = db.query(OutreachOrder).filter_by(id=order_id, user_id=user_id).first()
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
                order = db.query(OutreachOrder).filter_by(id=order_id, user_id=user_id).first()
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
            job["released"] = unused
            logger.info("[ENRICHMENT_JOB] %s: Refunded %d unused credits", job_id, unused)

        # Update order
        if order_id:
            order = db.query(OutreachOrder).filter_by(id=order_id, user_id=user_id).first()
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
                    job["released"] = to_refund
                    logger.info("[ENRICHMENT_JOB] %s: Refunded %d credits after failure", job_id, to_refund)
            except Exception:
                logger.error("[ENRICHMENT_JOB] %s: Failed to refund credits", job_id, exc_info=True)

        # Update order on failure
        if order_id:
            try:
                order = db.query(OutreachOrder).filter_by(id=order_id, user_id=user_id).first()
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

    # In test mode cap at 5 leads — avoids burning Apollo credits during staging tests.
    effective_limit = min(request.limit, 5) if settings.RAZORPAY_TEST_MODE else request.limit

    # Credit check (free tier bypasses)
    if effective_limit > 5:
        if not deduct_credits(db, current_user.id, effective_limit):
            raise HTTPException(
                status_code=402,
                detail="Insufficient credits. Please purchase an enrichment package first.",
            )
        db.commit()  # Commit credit deduction immediately

    # Create background job
    job_id = str(uuid.uuid4())[:8]
    _enrichment_jobs[job_id] = {
        "user_id": str(current_user.id),
        "reserved": effective_limit if effective_limit > 5 else 0,
        "status": "processing",
        "progress": "Starting enrichment...",
        "enriched": 0,
        "failed": 0,
        "total": min(effective_limit, unenriched_count),
        "error": "",
        "started_at": datetime.utcnow().isoformat(),
    }

    thread = threading.Thread(
        target=_run_enrichment_in_background,
        args=(job_id, request.candidate_id, effective_limit, str(current_user.id), request.order_id),
        daemon=True,
    )
    thread.start()

    logger.info("[ENRICHMENT] Job %s started for user %s, limit=%d (test_mode=%s)",
                job_id, current_user.id, effective_limit, settings.RAZORPAY_TEST_MODE)
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
def enrichment_status(
    job_id: str,
    current_user: User = Depends(get_current_user),
):
    """Poll for enrichment job progress. Only the job's owner may read it."""
    job = _enrichment_jobs.get(job_id)
    if not job or job.get("user_id") != str(current_user.id):
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
