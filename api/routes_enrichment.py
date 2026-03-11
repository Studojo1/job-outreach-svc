"""Enrichment Routes — Email enrichment via Apollo."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database.session import get_db
from database.models import User, Candidate
from services.enrichment.enrichment_service import enrich_contacts
from api.dependencies import get_current_user

router = APIRouter(prefix="/enrichment", tags=["Enrichment"])


class EnrichmentRequest(BaseModel):
    candidate_id: int
    limit: int = 200  # 200, 350, or 500


@router.post("/enrich")
async def enrich_leads(
    request: EnrichmentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Enrich leads with verified emails via Apollo People Match."""
    candidate = db.query(Candidate).filter_by(
        id=request.candidate_id, user_id=current_user.id
    ).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    try:
        result = enrich_contacts(
            db=db,
            candidate_id=request.candidate_id,
            limit=request.limit,
        )
        return {"status": "success", **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
