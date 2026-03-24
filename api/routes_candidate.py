"""Candidate Routes — Resume upload, profiling chat, and profile retrieval."""

import asyncio
import json as _json
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import Dict, Any, List, Optional
from pydantic import BaseModel

from database.session import get_db, SessionLocal
from database.models import User, Candidate, Lead, LeadScore
from services.candidate_intelligence.parser import parse_resume
from api.dependencies import get_current_user

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
    background_tasks: BackgroundTasks = BackgroundTasks(),
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

        # Extract resume intelligence in background — powers adaptive Q6 options
        from services.candidate_intelligence.resume_intelligence import extract_and_store_resume_profile
        background_tasks.add_task(
            extract_and_store_resume_profile,
            candidate_id=new_candidate.id,
            db_session_factory=SessionLocal,
        )

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
        from services.candidate_intelligence.profiler_agent import get_agent_response
        from services.candidate_intelligence.models import ChatMessage

        t_db = time.perf_counter()
        logger.info(f"[TIMING] DB lookup: {(t_db - t_start)*1000:.0f}ms")

        chat_history = [
            ChatMessage(role=msg["role"], content=msg["content"])
            for msg in request.chat_history
        ]
        chat_history.append(ChatMessage(role="user", content=request.message))

        # Run blocking LLM call in a thread to avoid blocking the event loop
        response = await asyncio.to_thread(
            get_agent_response,
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


@router.post("/{candidate_id}/chat/v2")
async def candidate_chat_fast(
    candidate_id: int,
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Fast profiling chat using pre-defined static questions — zero LLM calls per turn."""
    t_start = time.perf_counter()
    logger.info(f"[CHAT-V2] POST /candidate/{candidate_id}/chat/v2 — message='{request.message[:50]}'")

    candidate = db.query(Candidate).filter_by(id=candidate_id, user_id=current_user.id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    try:
        from services.candidate_intelligence._question_flow import get_active_questions, get_question

        # Collect user answers from chat history (frontend includes current msg in chat_history)
        user_answers = [
            m["content"] for m in request.chat_history
            if m["role"] == "user" and m["content"] != "__start__"
        ]

        # Build session by replaying answers to determine conditional question branching
        parsed_summary = candidate.parsed_json if isinstance(candidate.parsed_json, dict) else {}
        resume_skills = (
            parsed_summary.get("personal_info", {}).get("skills_detected", [])
            or parsed_summary.get("skills", [])
        )
        session = {
            "resume_uploaded": bool(candidate.resume_text),
            "resume_summary": {"skills": resume_skills},
            "answers": {},
        }
        for i, answer in enumerate(user_answers):
            active_qs = get_active_questions(session)
            if i < len(active_qs):
                session["answers"][active_qs[i]] = answer

        # Determine next question index
        active_qs = get_active_questions(session)
        q_index = len(user_answers)

        if q_index >= len(active_qs):
            t_end = time.perf_counter()
            logger.info(f"[CHAT-V2] Complete after {q_index} answers in {(t_end - t_start)*1000:.0f}ms")
            return {
                "message": "Got it! Generating your profile now...",
                "current_state": "PAYLOAD_READY",
                "mcq": None,
                "text_input": False,
                "is_complete": True,
                "questions_asked_so_far": q_index,
            }

        q_id = active_qs[q_index]
        q_def = get_question(q_id, session)

        # Build message: ack for previous answer + new question
        if request.message == "__start__":
            msg = q_def["message"]
        else:
            prev_q_id = active_qs[q_index - 1] if q_index > 0 else None
            ack = get_question(prev_q_id, session).get("ack") or "Got it." if prev_q_id else "Got it."
            msg = f"{ack}|||{q_def['message']}"

        t_end = time.perf_counter()
        logger.info(f"[CHAT-V2] Q{q_index + 1}/{len(active_qs)} ({q_id}) served in {(t_end - t_start)*1000:.0f}ms")

        return {
            "message": msg,
            "current_state": "MCQ" if q_def.get("mcq") else "TEXT",
            "mcq": q_def.get("mcq"),
            "text_input": q_def.get("text_input", False),
            "is_complete": False,
            "questions_asked_so_far": q_index + 1,
        }

    except Exception as e:
        logger.error(f"[CHAT-V2] Error for candidate {candidate_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{candidate_id}/chat/stream")
async def candidate_chat_stream(
    candidate_id: int,
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Fully deterministic streaming chat — zero LLM calls during quiz.
    All 7 questions served instantly from question_engine.py.
    Resume profile (extracted in background after upload) powers Q6 role options.
    Returns text/event-stream with 'complete' events only (no streaming chunks needed).
    """
    from services.candidate_intelligence.question_engine import (
        build_question_sequence, build_message
    )

    candidate = db.query(Candidate).filter_by(id=candidate_id, user_id=current_user.id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    # Reconstruct answer map by replaying chat history against question sequence
    raw_user_msgs = [
        m["content"] for m in request.chat_history
        if m["role"] == "user" and m["content"] != "__start__"
    ]

    # Build answers dict incrementally (needed because sequence depends on answers)
    answers: dict[str, str] = {}
    resume_profile = candidate.resume_profile if isinstance(candidate.resume_profile, dict) else {}
    resume_text = candidate.resume_text or ""

    for answer in raw_user_msgs:
        state = {"answers": answers, "resume_profile": resume_profile, "resume_text": resume_text}
        seq = build_question_sequence(state)
        answered_count = len(answers)
        if answered_count < len(seq):
            answers[seq[answered_count]["key"]] = answer

    # Build state for the *current* turn (current message not yet in answers)
    state = {"answers": answers, "resume_profile": resume_profile, "resume_text": resume_text}
    sequence = build_question_sequence(state)
    q_index = len(answers)  # index of next question to serve

    # ── Quiz complete ──────────────────────────────────────────────────
    if q_index >= len(sequence):
        payload = {
            "type": "complete",
            "message": "That's everything I need. Generating your profile now...",
            "current_state": "PAYLOAD_READY",
            "mcq": None,
            "text_input": False,
            "is_complete": True,
            "questions_asked_so_far": q_index,
        }
        logger.info(f"[STREAM] Quiz complete for candidate {candidate_id} after {q_index} answers")

        async def done_sse():
            yield f"data: {_json.dumps(payload)}\n\n"

        return StreamingResponse(
            done_sse(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── Serve next question instantly ─────────────────────────────────
    q_def = sequence[q_index]
    prev_key = sequence[q_index - 1]["key"] if q_index > 0 else None
    is_first = (request.message == "__start__" or q_index == 0)
    msg_text = build_message(q_def, prev_key, is_first)
    mcq = q_def.get("mcq")

    payload = {
        "type": "complete",
        "message": msg_text,
        "current_state": "MCQ" if mcq else "TEXT",
        "mcq": mcq,
        "text_input": q_def.get("text_input", False),
        "is_complete": False,
        "questions_asked_so_far": q_index + 1,
    }
    logger.info(f"[STREAM] Q{q_index + 1}/{len(sequence)} ({q_def['key']}) served instantly for candidate {candidate_id}")

    async def static_sse():
        yield f"data: {_json.dumps(payload)}\n\n"

    return StreamingResponse(
        static_sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
        from services.candidate_intelligence.profiler_agent import generate_final_payload
        from services.candidate_intelligence.models import ChatMessage

        logger.info(f"[PAYLOAD] chat_history length: {len(request.chat_history)}, resume_text length: {len(candidate.resume_text or '')}, parsed_json keys: {list(candidate.parsed_json.keys()) if isinstance(candidate.parsed_json, dict) else type(candidate.parsed_json)}")

        chat_history = [
            ChatMessage(role=msg["role"], content=msg["content"])
            for msg in request.chat_history
        ]

        logger.info(f"[PAYLOAD] Calling generate_final_payload with {len(chat_history)} messages")
        # Run blocking LLM call in a thread to avoid blocking the event loop
        payload = await asyncio.to_thread(
            generate_final_payload,
            chat_history=chat_history,
            resume_summary=candidate.parsed_json,
            resume_raw_text=candidate.resume_text,
            resume_uploaded=True,
        )

        t_llm = time.perf_counter()
        logger.info(f"[TIMING] Payload generation LLM: {(t_llm - t_start)*1000:.0f}ms")

        # Store profile in candidate record
        payload_dict = payload.dict() if hasattr(payload, 'dict') else payload.model_dump()
        logger.info(f"[PAYLOAD] Generated payload keys: {list(payload_dict.keys())}")
        candidate.parsed_json = payload_dict
        if payload.career_analysis and payload.career_analysis.recommended_roles:
            candidate.target_roles = [r.title for r in payload.career_analysis.recommended_roles]
            logger.info(f"[PAYLOAD] target_roles: {candidate.target_roles}")
        if payload.preferences and payload.preferences.industry_interests:
            candidate.target_industries = payload.preferences.industry_interests
            logger.info(f"[PAYLOAD] target_industries: {candidate.target_industries}")
        db.commit()

        t_end = time.perf_counter()
        logger.info(f"[TIMING] Total payload request: {(t_end - t_start)*1000:.0f}ms — SUCCESS")

        return {
            "status": "success",
            "payload": payload_dict,
        }
    except Exception as e:
        import traceback
        logger.error(f"[PAYLOAD] FAILED for candidate {candidate_id} after {(time.perf_counter() - t_start)*1000:.0f}ms: {type(e).__name__}: {e}")
        logger.error(f"[PAYLOAD] Traceback:\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Profile generation failed: {type(e).__name__}: {str(e)}")


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
