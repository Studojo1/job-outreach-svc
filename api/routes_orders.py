"""Order Routes — Outreach order tracking and resumable workflow."""

import logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from database.session import get_db
from database.models import (
    User, OutreachOrder, Candidate, Campaign, EmailAccount, Lead,
)
from sqlalchemy import func
from api.dependencies import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["Orders"])

# Valid state transitions (JIT: enrichment happens during campaign_running, not as a separate step)
VALID_TRANSITIONS = {
    "created": ["leads_generating", "profile_complete"],
    "profile_complete": ["leads_generating", "leads_ready"],
    "leads_generating": ["leads_ready"],
    "leads_ready": ["campaign_setup"],
    "campaign_setup": ["email_connected"],
    "email_connected": ["campaign_running"],
    "campaign_running": ["completed"],
    # Legacy states still accepted for backward compat with existing orders
    "enriching": ["enrichment_complete", "leads_ready", "campaign_setup"],
    "enrichment_complete": ["campaign_setup"],
}


class OrderCreateRequest(BaseModel):
    candidate_id: Optional[int] = None


class OrderUpdateRequest(BaseModel):
    status: Optional[str] = None
    candidate_id: Optional[int] = None
    campaign_id: Optional[int] = None
    email_account_id: Optional[int] = None
    leads_collected: Optional[int] = None
    linkedin_campaign_id: Optional[int] = None
    linkedin_connected: Optional[bool] = None
    log_entry: Optional[str] = None


def _append_log(order: OutreachOrder, message: str):
    """Append a timestamped entry to the order's action log."""
    log = list(order.action_log or [])
    log.append({"ts": datetime.utcnow().isoformat(), "msg": message})
    order.action_log = log


@router.post("/create")
async def create_order(
    request: OrderCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new outreach order for the current user."""
    order = OutreachOrder(
        user_id=current_user.id,
        candidate_id=request.candidate_id,
        status="created",
    )
    _append_log(order, "Order created")

    # Auto-advance status based on what the candidate has already completed
    if request.candidate_id:
        candidate = db.query(Candidate).filter(Candidate.id == request.candidate_id).first()
        if candidate:
            lead_count = db.query(func.count()).select_from(Lead)\
                .filter(Lead.candidate_id == request.candidate_id).scalar() or 0
            if lead_count > 0:
                order.status = "leads_ready"
                _append_log(order, f"Auto-advanced to leads_ready ({lead_count} leads found)")
            else:
                order.status = "profile_complete"
                _append_log(order, "Auto-advanced to profile_complete (onboarding done, no leads yet)")

    db.add(order)
    db.commit()
    db.refresh(order)

    logger.info("[ORDER] Created order %d for user %s", order.id, current_user.id)
    return {"order_id": order.id, "status": order.status}


@router.get("/active")
async def get_active_order(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the user's most recent non-completed order, if any.

    This is the primary endpoint the frontend uses to check if the user
    has an in-progress outreach run they can resume.
    """
    order = (
        db.query(OutreachOrder)
        .filter(
            OutreachOrder.user_id == current_user.id,
            OutreachOrder.status != "completed",
        )
        .order_by(OutreachOrder.created_at.desc())
        .first()
    )

    if not order:
        return {"order": None}

    _heal_candidate_binding(db, order)
    return _serialize_order(order)


@router.get("/list")
async def list_orders(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return all outreach orders for the current user (for My Orders page)."""
    orders = (
        db.query(OutreachOrder)
        .filter(OutreachOrder.user_id == current_user.id)
        .order_by(OutreachOrder.created_at.desc())
        .all()
    )

    for o in orders:
        _heal_candidate_binding(db, o)
    return {"orders": [_serialize_order(o) for o in orders]}


@router.get("/{order_id}")
async def get_order(
    order_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return a specific order's details."""
    order = db.query(OutreachOrder).filter_by(id=order_id, user_id=current_user.id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    _heal_candidate_binding(db, order)
    return _serialize_order(order)


@router.post("/{order_id}/update")
async def update_order(
    order_id: int,
    request: OrderUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update an order's state, linked IDs, or append a log entry."""
    order = db.query(OutreachOrder).filter_by(id=order_id, user_id=current_user.id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if request.status and request.status != order.status:
        allowed = VALID_TRANSITIONS.get(order.status, [])
        if request.status not in allowed:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot transition from '{order.status}' to '{request.status}'. Allowed: {allowed}",
            )
        old_status = order.status
        order.status = request.status
        _append_log(order, f"Status: {old_status} → {request.status}")



    if request.candidate_id is not None:
        order.candidate_id = request.candidate_id
    if request.campaign_id is not None:
        order.campaign_id = request.campaign_id
    if request.email_account_id is not None:
        order.email_account_id = request.email_account_id
    if request.leads_collected is not None:
        order.leads_collected = request.leads_collected
    if request.linkedin_campaign_id is not None:
        order.linkedin_campaign_id = request.linkedin_campaign_id
    if request.linkedin_connected:
        order.linkedin_connected_at = datetime.utcnow()

    if request.log_entry:
        _append_log(order, request.log_entry)

    # Trigger preview enrichment when in campaign_setup (new or existing orders).
    # Guard: only fires if fewer than 5 leads are enriched, preventing redundant Apollo calls.
    if order.status == "campaign_setup" and order.candidate_id:
        from database.models import Lead as _Lead
        already_enriched = db.query(_Lead).filter(
            _Lead.candidate_id == order.candidate_id,
            _Lead.email.isnot(None),
            _Lead.email_verified == True,
        ).count()
        if already_enriched < 5:
            import threading
            from services.enrichment.enrichment_service import enrich_preview_leads
            threading.Thread(
                target=enrich_preview_leads,
                args=(order.candidate_id,),
                daemon=True,
            ).start()
            logger.info("[ORDER] Triggered preview enrichment for candidate %d (enriched=%d)",
                        order.candidate_id, already_enriched)

    order.updated_at = datetime.utcnow()
    db.commit()

    logger.info("[ORDER] Updated order %d — status=%s", order.id, order.status)
    return _serialize_order(order)


def _has_mailbox(db: Session, user_id: str) -> bool:
    return db.query(EmailAccount.id).filter(EmailAccount.user_id == user_id).first() is not None


def _has_paid_credits(db: Session, user_id: str) -> bool:
    """True if the user has credits left to spend, i.e. they have already paid."""
    from database.models import UserCredit
    row = db.query(UserCredit).filter(UserCredit.user_id == user_id).first()
    if not row:
        return False
    return (row.total_credits or 0) - (row.used_credits or 0) > 0


@router.get("/{order_id}/resume")
async def resume_order(
    order_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the redirect path for resuming an in-progress order.

    Maps order status to the appropriate frontend page:
      created / leads_generating  → /onboarding/upload (or /leads/discovery if candidate exists)
      leads_ready                 → /leads/results
      campaign_setup              → /campaign/setup
      email_connected             → /campaign/setup
      campaign_running            → /campaign/dashboard
      completed                   → /campaign/dashboard
    """
    order = db.query(OutreachOrder).filter_by(id=order_id, user_id=current_user.id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    status = order.status
    plan_type = getattr(order, "plan_type", "email") or "email"

    if status in ("created", "leads_generating"):
        if order.candidate_id:
            redirect = "/leads/discovery"
        else:
            redirect = "/onboarding/upload"
    elif status == "leads_ready":
        redirect = "/leads/results"
    elif status in ("enriching", "enrichment_complete"):
        order.status = "campaign_setup"
        order.updated_at = datetime.utcnow()
        _append_log(order, f"Auto-corrected: {status} → campaign_setup (JIT enrichment)")
        db.commit()
        redirect = "/connect/gmail" if plan_type != "linkedin" else "/connect/linkedin"
    elif status == "campaign_setup":
        # Route to correct connect page based on plan channel.
        # If LinkedIn is already connected (handleSuccess set linkedin_connected_at),
        # send the user to the safety review page instead of back to connect.
        if plan_type == "linkedin":
            if getattr(order, "linkedin_connected_at", None) and getattr(order, "linkedin_campaign_id", None):
                redirect = "/campaign/linkedin-safety"
            else:
                redirect = "/connect/linkedin"
        elif _has_paid_credits(db, order.user_id) and not _has_mailbox(db, order.user_id):
            # Already paid, just never connected a mailbox. That is the single
            # biggest place paying users stall, so send them straight at the one
            # thing that is actually blocking them rather than to a landing page
            # or back through pricing they have already cleared.
            redirect = "/connect/gmail"
        else:
            redirect = "/campaign/setup"
    elif status == "email_connected":
        if plan_type == "both" and not getattr(order, "linkedin_connected_at", None):
            redirect = "/connect/linkedin"
        elif plan_type == "both" and getattr(order, "linkedin_campaign_id", None):
            redirect = "/campaign/linkedin-safety"
        else:
            redirect = "/campaign/setup"
    elif status in ("campaign_running", "completed"):
        redirect = "/campaign/dashboard"
    else:
        redirect = "/onboarding/upload"

    return {
        "redirect": redirect,
        "order_id": order.id,
        "status": order.status,
        "candidate_id": order.candidate_id,
        "campaign_id": order.campaign_id,
        "email_account_id": order.email_account_id,
    }


class FunnelStageRequest(BaseModel):
    """Request body for the generic funnel-stage ping. The frontend uses this
    to mark stages that don't have a natural backend trigger (e.g., 'user
    landed on the pricing page', which is a pure frontend event)."""
    stage: str


# Whitelist of stages the frontend is allowed to ping. Other stages are
# authoritatively set by the backend (leads_generated, payment_made, etc.)
# and shouldn't be settable from the client.
_FRONTEND_SETTABLE_STAGES = {"payment_page_reached"}


@router.post("/funnel/mark")
async def mark_funnel_stage(
    request: FunnelStageRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark a frontend-driven funnel stage on the user's active OutreachOrder.

    Used for stages that fire on a page-mount or button-click rather than a
    backend operation. Idempotent — safe to call repeatedly. Only stages on
    the whitelist may be set this way; others are server-authoritative.
    """
    if request.stage not in _FRONTEND_SETTABLE_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Stage '{request.stage}' is not frontend-settable",
        )
    from services.stage_tracking import safe_mark_stage
    safe_mark_stage(db, str(current_user.id), request.stage)
    return {"ok": True, "stage": request.stage}


# Statuses that assert discovery has already happened. Only these can be healed;
# an order earlier than this is allowed to have an empty candidate.
_LEADS_EXPECTED_STATUSES = {
    "leads_ready", "campaign_setup", "email_connected", "campaign_running",
    "enriching", "enrichment_complete", "completed",
}


def _heal_candidate_binding(db: Session, order: OutreachOrder) -> None:
    """Repoint an order that is bound to a candidate holding no leads.

    Re-onboarding creates a fresh candidate. If that happens after discovery has
    already run, the order follows the new empty record and the dashboard renders
    nothing, even for a user who has paid and had leads generated.

    `_resolve_effective_candidate` in routes_campaign already repairs this, but it
    only runs on campaign operations, so the leads dashboard never benefited. Heal
    it here instead, on the single path every order read goes through.

    Only ever moves an order OFF a candidate with zero leads, and only ONTO a
    candidate belonging to the same user that actually has leads. An order whose
    candidate has leads is never touched.

    Critically, this only applies to orders that have already passed discovery.
    A user starting a SECOND run has a brand new candidate with legitimately zero
    leads, and healing that would drag the new order back onto the old candidate.
    Before leads_ready, empty is the expected state, so leave it alone.
    """
    if not order.candidate_id:
        return
    if order.status not in _LEADS_EXPECTED_STATUSES:
        return
    bound_leads = (
        db.query(func.count(Lead.id)).filter(Lead.candidate_id == order.candidate_id).scalar() or 0
    )
    if bound_leads:
        return

    best = (
        db.query(Lead.candidate_id, func.count(Lead.id).label("n"))
        .join(Candidate, Candidate.id == Lead.candidate_id)
        .filter(Candidate.user_id == order.user_id)
        .group_by(Lead.candidate_id)
        .order_by(func.count(Lead.id).desc(), Lead.candidate_id.desc())
        .first()
    )
    if not best or not best[1]:
        return

    logger.warning(
        "[ORDER-HEAL] order %s was bound to candidate %s with 0 leads; repointing to %s (%d leads)",
        order.id, order.candidate_id, best[0], best[1],
    )
    order.candidate_id = best[0]
    try:
        db.commit()
    except Exception:
        db.rollback()


def _serialize_order(order: OutreachOrder) -> dict:
    return {
        "order": {
            "id": order.id,
            "status": order.status,
            "plan_type": getattr(order, "plan_type", "email") or "email",
            "candidate_id": order.candidate_id,
            "campaign_id": order.campaign_id,
            "email_account_id": order.email_account_id,
            "linkedin_campaign_id": getattr(order, "linkedin_campaign_id", None),
            "linkedin_connected_at": (
                order.linkedin_connected_at.isoformat()
                if getattr(order, "linkedin_connected_at", None) else None
            ),
            "linkedin_credits_reserved": getattr(order, "linkedin_credits_reserved", 0),
            "linkedin_credits_used": getattr(order, "linkedin_credits_used", 0),
            "leads_collected": order.leads_collected,
            "leads_target": order.leads_target,
            "action_log": order.action_log or [],
            "created_at": order.created_at.isoformat() if order.created_at else None,
            "updated_at": order.updated_at.isoformat() if order.updated_at else None,
        }
    }
