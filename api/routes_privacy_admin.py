"""Admin: the suppression list and third-party removal requests.

Privacy Policy v2.0 §5 and Terms §6. Mounted under /api/v1/admin/outreach.

  GET  /suppression?search=&limit=&offset=
  POST /suppression                        {"email"}
  GET  /removal-requests?status=open|done|all
  POST /removal-requests                   {"email", "received_on": "YYYY-MM-DD"}
  POST /removal-requests/{id}/delete-data
"""

from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import inspect, or_
from sqlalchemy.orm import Session

from api.dependencies import get_admin_user
from database.models import RemovalRequest, SuppressedEmail, User
from database.session import get_db
from services import removal_requests as rr
from services.email_campaign import suppression

router = APIRouter(prefix="/admin/outreach", tags=["admin"])


def _require_055(db: Session) -> None:
    insp = inspect(db.get_bind())
    tables = set(insp.get_table_names())
    if "removal_requests" not in tables or "email_hash" not in {
        c["name"] for c in insp.get_columns("suppressed_emails")
    }:
        raise HTTPException(status_code=503, detail="Migration 055 is not applied on this database yet.")


def _iso(v):
    return v.isoformat() if v else None


def _suppression_item(row: SuppressedEmail) -> dict:
    return {
        "id": row.id,
        # Bounced addresses are shown in full (they are ours to debug); people
        # who asked to be removed are masked; hashed-only entries have none.
        "email": row.email if row.source == "bounce" else suppression.mask(row.email),
        "source": row.source,
        "suppressed_at": _iso(row.suppressed_at),
        "hash_only": row.email is None,
    }


@router.get("/suppression")
def list_suppression(
    search: str = Query("", max_length=320),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    _require_055(db)
    q = db.query(SuppressedEmail)
    s = search.strip().lower()
    if s:
        # A full address also finds its hashed-only entry.
        conds = [SuppressedEmail.email.ilike(f"%{s}%")]
        if "@" in s:
            conds.append(SuppressedEmail.email_hash == suppression.email_hash(s))
        q = q.filter(or_(*conds))
    total = q.count()
    rows = q.order_by(SuppressedEmail.suppressed_at.desc(), SuppressedEmail.id.desc()).offset(offset).limit(limit).all()
    return {"total": total, "items": [_suppression_item(r) for r in rows]}


class EmailBody(BaseModel):
    email: str


@router.post("/suppression")
def add_suppression(
    body: EmailBody,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    _require_055(db)
    try:
        a = rr.valid_address(body.email)
    except rr.RemovalError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    h = suppression.email_hash(a)
    existed = db.query(SuppressedEmail).filter_by(email_hash=h).first() is not None
    suppression.suppress(db, a, f"added by admin {admin.email}", source="manual")
    db.commit()
    row = db.query(SuppressedEmail).filter_by(email_hash=h).one()
    return {**_suppression_item(row), "created": not existed}


@router.get("/removal-requests")
def list_removal_requests(
    status: Literal["open", "done", "all"] = Query("open"),
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    _require_055(db)
    q = db.query(RemovalRequest)
    if status != "all":
        q = q.filter(RemovalRequest.status == status)
    if status == "open":
        q = q.order_by(RemovalRequest.deadline.asc(), RemovalRequest.id.asc())
    else:
        q = q.order_by(RemovalRequest.received_at.desc(), RemovalRequest.id.desc())
    return {"items": [rr.as_item(r) for r in q.all()]}


class RemovalBody(BaseModel):
    email: str
    received_on: date


@router.post("/removal-requests")
def create_removal_request(
    body: RemovalBody,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    _require_055(db)
    if body.received_on > datetime.now(timezone.utc).date():
        raise HTTPException(status_code=400, detail="received_on cannot be in the future.")
    try:
        req = rr.log_request(db, body.email, body.received_on, actor=admin.email or str(admin.id))
    except rr.RemovalError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return {**rr.as_item(req), "suppressed": True}


@router.post("/removal-requests/{request_id}/delete-data")
def delete_removal_request_data(
    request_id: int,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    _require_055(db)
    try:
        counts = rr.delete_data(db, request_id, actor=admin.email or str(admin.id))
    except LookupError:
        raise HTTPException(status_code=404, detail="Removal request not found.") from None
    except rr.RemovalError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    req = db.get(RemovalRequest, request_id)
    return {**rr.as_item(req), "deleted": counts}
