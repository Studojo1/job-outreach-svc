"""Candidate Routes — Resume upload, profiling chat, and profile retrieval."""

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from typing import Dict, Any, List, Optional
from pydantic import BaseModel

from job_outreach_tool.database.session import get_db
from job_outreach_tool.database.models import User, Candidate, Lead, LeadScore
from job_outreach_tool.services.candidate_intelligence.parser import parse_resume
from job_outreach_tool.api.dependencies import get_current_user

import logging
import time
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/candidate", tags=["Candidate"])


class ChatRequest(BaseModel):
    message: str
    chat_history: List[Dict[str, str]] = []


@router.post("/upload")
async def upload_resume(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload and parse a resume. Returns raw text and metadata preview."""
    contents = await file.read()
    try:
        raw_text, preview = parse_resume(contents, file.filename)

        new_candidate = Candidate(
            user_id=current_user.id,
            resume_text=raw_text,
            parsed_json=preview,
        )
        db.add(new_candidate)
        db.commit()
        db.refresh(new_candidate)

        return {
            "status": "success",
            "candidate_id": new_candidate.id,
            "preview": preview,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{candidate_id}/chat")
async def candidate_chat(
    candidate_id: int,
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Send a message to the profiling agent and get the next response."""
    t_start = time.perf_counter()
    logger.info(f"[CHAT] POST /candidate/{candidate_id}/chat — message='{request.message[:50]}'")

    candidate = db.query(Candidate).filter_by(id=candidate_id, user_id=current_user.id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    try:
        from job_outreach_tool.services.candidate_intelligence.profiler_agent import get_agent_response
        from job_outreach_tool.services.candidate_intelligence.models import ChatMessage

        t_db = time.perf_counter()
        logger.info(f"[TIMING] DB lookup: {(t_db - t_start)*1000:.0f}ms")

        chat_history = [
            ChatMessage(role=msg["role"], content=msg["content"])
            for msg in request.chat_history
        ]
        chat_history.append(ChatMessage(role="user", content=request.message))

        response = get_agent_response(
            chat_history=chat_history,
            resume_summary=candidate.parsed_json,
            resume_raw_text=candidate.resume_text,
        )

        t_end = time.perf_counter()
        logger.info(f"[TIMING] Total chat request: {(t_end - t_start)*1000:.0f}ms")

        mcq_dict = None
        if response.mcq:
            mcq_dict = response.mcq.model_dump() if hasattr(response.mcq, 'model_dump') else response.mcq.dict()

        return {
            "message": response.message,
            "current_state": response.current_state,
            "mcq": mcq_dict,
            "text_input": response.text_input,
            "is_complete": response.is_complete,
            "questions_asked_so_far": response.questions_asked_so_far,
        }
    except Exception as e:
        logger.error(f"Chat error for candidate {candidate_id} after {(time.perf_counter() - t_start)*1000:.0f}ms: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{candidate_id}/generate-payload")
async def generate_payload(
    candidate_id: int,
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate the final candidate profile payload after chat completion."""
    t_start = time.perf_counter()
    logger.info(f"[PAYLOAD] POST /candidate/{candidate_id}/generate-payload")

    candidate = db.query(Candidate).filter_by(id=candidate_id, user_id=current_user.id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    try:
        from job_outreach_tool.services.candidate_intelligence.profiler_agent import generate_final_payload
        from job_outreach_tool.services.candidate_intelligence.models import ChatMessage

        chat_history = [
            ChatMessage(role=msg["role"], content=msg["content"])
            for msg in request.chat_history
        ]

        payload = generate_final_payload(
            chat_history=chat_history,
            resume_summary=candidate.parsed_json,
            resume_raw_text=candidate.resume_text,
            resume_uploaded=True,
        )

        t_llm = time.perf_counter()
        logger.info(f"[TIMING] Payload generation LLM: {(t_llm - t_start)*1000:.0f}ms")

        # Store profile in candidate record
        candidate.parsed_json = payload.dict() if hasattr(payload, 'dict') else payload.model_dump()
        if payload.career_analysis and payload.career_analysis.recommended_roles:
            candidate.target_roles = [r.title for r in payload.career_analysis.recommended_roles]
        if payload.preferences and payload.preferences.industry_interests:
            candidate.target_industries = payload.preferences.industry_interests
        db.commit()

        t_end = time.perf_counter()
        logger.info(f"[TIMING] Total payload request: {(t_end - t_start)*1000:.0f}ms")

        return {
            "status": "success",
            "payload": payload.dict() if hasattr(payload, 'dict') else payload.model_dump(),
        }
    except Exception as e:
        logger.error(f"Payload generation error for candidate {candidate_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{candidate_id}/profile")
async def get_candidate_profile(
    candidate_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get the candidate's parsed profile."""
    candidate = db.query(Candidate).filter_by(id=candidate_id, user_id=current_user.id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    return {
        "candidate_id": candidate.id,
        "parsed_json": candidate.parsed_json,
        "target_roles": candidate.target_roles,
        "target_industries": candidate.target_industries,
        "created_at": candidate.created_at.isoformat() if candidate.created_at else None,
    }


@router.get("/{candidate_id}/leads")
async def get_candidate_leads(
    candidate_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get all leads for a candidate with their scores."""
    logger.info(f"[LeadSearch] GET /candidate/{candidate_id}/leads — user_id={current_user.id}")

    candidate = db.query(Candidate).filter_by(
        id=candidate_id, user_id=current_user.id
    ).first()
    if not candidate:
        logger.warning(f"[LeadSearch] Candidate {candidate_id} not found for user {current_user.id}")
        raise HTTPException(status_code=404, detail="Candidate not found")

    leads = db.query(Lead).filter_by(candidate_id=candidate_id).all()
    logger.info(f"[LeadSearch] Leads retrieved from DB: {len(leads)}")

    results = []
    for lead in leads:
        score = db.query(LeadScore).filter_by(lead_id=lead.id).first()
        results.append({
            "id": lead.id,
            "name": lead.name,
            "title": lead.title,
            "company": lead.company,
            "industry": lead.industry,
            "location": lead.location,
            "linkedin_url": lead.linkedin_url,
            "email": lead.email,
            "email_verified": lead.email_verified,
            "company_size": lead.company_size,
            "status": lead.status,
            "score": {
                "overall": score.overall_score,
                "title_relevance": score.title_relevance,
                "department_relevance": score.department_relevance,
                "industry_relevance": score.industry_relevance,
                "seniority_relevance": score.seniority_relevance,
                "location_relevance": score.location_relevance,
                "explanation": score.explanation,
            } if score else None,
        })

    # Sort by score descending
    results.sort(key=lambda x: (x["score"]["overall"] if x["score"] else 0), reverse=True)

    logger.info(f"[LeadSearch] Returning {len(results)} leads to frontend (scored: {sum(1 for r in results if r['score'])})")

    return {"leads": results, "total": len(results)}
