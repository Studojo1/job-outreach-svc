"""Scoring Routes — Lead scoring against candidate profile."""

import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from job_outreach_tool.database.session import get_db
from job_outreach_tool.database.models import User, Candidate
from job_outreach_tool.api.dependencies import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scoring", tags=["Scoring"])


@router.post("/{candidate_id}/score")
async def score_leads(
    candidate_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Score all leads for a candidate."""
    candidate = db.query(Candidate).filter_by(
        id=candidate_id, user_id=current_user.id
    ).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    try:
        from job_outreach_tool.api.routes_discovery import _score_candidate_leads
        scored_count = _score_candidate_leads(db, candidate)
        return {"status": "success", "scored_count": scored_count}
    except Exception as e:
        logger.error(f"[SCORING] Error scoring candidate {candidate_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
