"""Order Routes — Outreach order tracking and resumable workflow."""

import logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from database.session import get_db
from database.models import (
    User, OutreachOrder, Candidate, Campaign, EmailAccount,
)
from api.dependencies import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["Orders"])

# Valid state transitions (JIT: enrichment happens during campaign_running, not as a separate step)
VALID_TRANSITIONS = {
    "created": ["leads_generating"],
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
    if status in ("created", "leads_generating"):
        if order.candidate_id:
            redirect = "/leads/discovery"
        else:
            redirect = "/onboarding/upload"
    elif status == "leads_ready":
        # JIT: skip enrichment, go to campaign setup (payment page)
        redirect = "/leads/results"
    elif status in ("enriching", "enrichment_complete"):
        # Legacy orders still in enrichment states — auto-advance to campaign setup
        order.status = "campaign_setup"
        order.updated_at = datetime.utcnow()
        _append_log(order, f"Auto-corrected: {status} → campaign_setup (JIT enrichment)")
        db.commit()
        redirect = "/connect/gmail"
    elif status in ("campaign_setup", "email_connected"):
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


def _serialize_order(order: OutreachOrder) -> dict:
    return {
        "order": {
            "id": order.id,
            "status": order.status,
            "candidate_id": order.candidate_id,
            "campaign_id": order.campaign_id,
            "email_account_id": order.email_account_id,
            "leads_collected": order.leads_collected,
            "leads_target": order.leads_target,
            "action_log": order.action_log or [],
            "created_at": order.created_at.isoformat() if order.created_at else None,
            "updated_at": order.updated_at.isoformat() if order.updated_at else None,
        }
    }
