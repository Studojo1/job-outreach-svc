"""Candidate Routes — Resume upload, profiling chat, and profile retrieval."""

import asyncio
import json as _json
import queue
import threading
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import Dict, Any, List, Optional
from pydantic import BaseModel

from database.session import get_db
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


# ============================================================================
# Static question definitions for /chat/stream hybrid endpoint
# ============================================================================

_STATIC_QUESTIONS = [
    {   # Q1 — career stage (also served client-side in chat.tsx — keep options in sync)
        "key": "stage",
        "ack": None,
        "message": "Let's map out your career goals! Which of these best describes where you are right now?",
        "mcq": {
            "question": "Which best describes you right now?",
            "options": [
                {"label": "A", "text": "Student, not graduating soon"},
                {"label": "B", "text": "Student, graduating within 6 months"},
                {"label": "C", "text": "Recent graduate (0-2 years exp.)"},
                {"label": "D", "text": "Experienced professional (3+ years)"},
                {"label": "E", "text": "Switching careers / exploring new fields"},
                {"label": "F", "text": "Other"},
            ],
            "allow_multiple": False,
        },
        "text_input": False,
    },
    {   # Q2 — job type (skipped for experienced/switching)
        "key": "job_type",
        "ack": "Got it!",
        "message": "Are you targeting an internship or a full-time role?",
        "mcq": {
            "question": "What type of opportunity are you looking for?",
            "options": [
                {"label": "A", "text": "Full-time job"},
                {"label": "B", "text": "Internship (3-6 months)"},
                {"label": "C", "text": "Part-time or freelance"},
                {"label": "D", "text": "Open to all options"},
                {"label": "E", "text": "Other"},
            ],
            "allow_multiple": False,
        },
        "text_input": False,
    },
    {   # Q3 — location (NLP-personalized with detected city)
        "key": "location",
        "ack": "Got it!",
        "message": "Which cities or regions would you prefer to work in?",
        "mcq": {
            "question": "Preferred work locations?",
            "options": [
                {"label": "A", "text": "Bengaluru"},
                {"label": "B", "text": "Mumbai"},
                {"label": "C", "text": "Delhi NCR"},
                {"label": "D", "text": "Hyderabad"},
                {"label": "E", "text": "Pune"},
                {"label": "F", "text": "Remote"},
                {"label": "G", "text": "Open to relocate / International"},
                {"label": "H", "text": "Other"},
            ],
            "allow_multiple": True,
        },
        "text_input": False,
    },
    {   # Q4 — company stage
        "key": "company_stage",
        "ack": "Makes sense.",
        "message": "What kind of company environment appeals to you most?",
        "mcq": {
            "question": "Company type?",
            "options": [
                {"label": "A", "text": "Early-stage startup (seed, under 50 people)"},
                {"label": "B", "text": "Growth-stage startup (50-500 people)"},
                {"label": "C", "text": "Mid-size company (500-2000)"},
                {"label": "D", "text": "Large enterprise or MNC (2000+)"},
                {"label": "E", "text": "No strong preference"},
                {"label": "F", "text": "Other"},
            ],
            "allow_multiple": False,
        },
        "text_input": False,
    },
    {   # Q5 — open text, no LLM needed
        "key": "career_goal",
        "ack": "Good to know.",
        "message": "What's the one thing you really want from your next role? e.g. 'Learn to close enterprise deals', 'Build something people actually use', 'Hit 15-20 LPA fast'",
        "mcq": None,
        "text_input": True,
    },
]

# City keywords → display name for NLP location personalization
_CITY_KEYWORDS: dict[str, str] = {
    "chennai": "Chennai", "madras": "Chennai",
    "kolkata": "Kolkata", "calcutta": "Kolkata",
    "ahmedabad": "Ahmedabad", "chandigarh": "Chandigarh",
    "kochi": "Kochi", "cochin": "Kochi", "jaipur": "Jaipur",
    "new york": "New York", "nyc": "New York",
    "san francisco": "San Francisco", "bay area": "San Francisco",
    "london": "London", "singapore": "Singapore",
    "dubai": "Dubai", "toronto": "Toronto",
    "sydney": "Sydney", "amsterdam": "Amsterdam",
    "berlin": "Berlin", "tokyo": "Tokyo",
}


def _personalize_location_options(options: list[dict], resume_text: str | None) -> list[dict]:
    """Insert detected city into location options if not already present."""
    if not resume_text:
        return options
    text_lower = resume_text[:800].lower()
    existing = {opt["text"] for opt in options}
    for keyword, city in _CITY_KEYWORDS.items():
        if keyword in text_lower and city not in existing:
            new_opts = list(options)
            insert_pos = len(new_opts) - 2  # before Remote and Other
            new_opts.insert(insert_pos, {"label": "", "text": city})
            # Re-label A, B, C...
            for i, opt in enumerate(new_opts):
                new_opts[i] = {**opt, "label": chr(65 + i)}
            return new_opts
    return options


def _build_effective_question_list(user_answers: list[str]) -> list[dict]:
    """
    Build the active static question list, skipping job_type for
    experienced professionals and career switchers.
    """
    questions = list(_STATIC_QUESTIONS)
    if user_answers:
        stage = user_answers[0].lower()
        # Match new option texts (no em dashes): "Experienced professional (3+ years)"
        # and "Switching careers / exploring new fields"
        if any(kw in stage for kw in ("experienced", "3+", "switching")):
            questions = [q for q in questions if q["key"] != "job_type"]
    return questions


@router.post("/{candidate_id}/chat/stream")
async def candidate_chat_stream(
    candidate_id: int,
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Hybrid streaming chat endpoint:
    - Q1–Q5: served instantly from static definitions (NLP-personalized where useful)
    - Q6–Q7: LLM with compact context + SSE streaming
    Returns text/event-stream with 'chunk' and 'complete' events.
    """
    candidate = db.query(Candidate).filter_by(id=candidate_id, user_id=current_user.id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    # Collect user answers from chat history (excludes __start__)
    user_answers = [
        m["content"] for m in request.chat_history
        if m["role"] == "user" and m["content"] != "__start__"
    ]

    effective_qs = _build_effective_question_list(user_answers)
    q_index = len(user_answers)  # which question slot to serve next

    # ── Static question phase ──────────────────────────────────────────
    if q_index < len(effective_qs):
        q_def = dict(effective_qs[q_index])
        mcq = q_def.get("mcq")

        # NLP: personalize location options with city detected from resume
        if q_def["key"] == "location" and mcq and candidate.resume_text:
            mcq = dict(mcq)
            mcq["options"] = _personalize_location_options(mcq["options"], candidate.resume_text)

        # Build message: ack for previous answer + this question
        if request.message == "__start__":
            msg_text = q_def["message"]
        else:
            ack = (effective_qs[q_index - 1].get("ack") or "Got it.") if q_index > 0 else "Got it."
            msg_text = f"{ack}|||{q_def['message']}"

        payload = {
            "type": "complete",
            "message": msg_text,
            "current_state": "MCQ" if mcq else "TEXT",
            "mcq": mcq,
            "text_input": q_def.get("text_input", False),
            "is_complete": False,
            "questions_asked_so_far": q_index + 1,
        }
        logger.info(f"[STREAM] Static Q{q_index + 1} ({q_def['key']}) served instantly")

        async def static_sse():
            yield f"data: {_json.dumps(payload)}\n\n"

        return StreamingResponse(
            static_sse(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── LLM question phase (Q6, Q7) — reduced context + SSE stream ────
    llm_q_index = q_index - len(effective_qs)  # 0 = Q6, 1 = Q7

    # After 2 LLM questions answered → complete
    if llm_q_index >= 2:
        payload = {
            "type": "complete",
            "message": "That's everything I need — generating your profile now...",
            "current_state": "PAYLOAD_READY",
            "mcq": None,
            "text_input": False,
            "is_complete": True,
            "questions_asked_so_far": q_index + 1,
        }

        async def done_sse():
            yield f"data: {_json.dumps(payload)}\n\n"

        return StreamingResponse(
            done_sse(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    from services.candidate_intelligence.profiler_agent import stream_agent_response
    from services.candidate_intelligence.models import ChatMessage as ProfChatMessage

    # Compact resume summary for fast context
    raw_json = candidate.parsed_json if isinstance(candidate.parsed_json, dict) else {}
    resume_summary = {
        "name": raw_json.get("name"),
        "skills": raw_json.get("skills", []),
        "experience_years": raw_json.get("experience_years"),
        "education": raw_json.get("education", []),
    }

    chat_history = [
        ProfChatMessage(role=m["role"], content=m["content"])
        for m in request.chat_history
    ]
    chat_history.append(ProfChatMessage(role="user", content=request.message))

    # Bridge blocking OpenAI stream to async SSE via thread queue
    chunk_q: queue.Queue = queue.Queue(maxsize=200)

    def producer():
        try:
            for event_type, data in stream_agent_response(
                    chat_history, resume_summary, llm_phase_start=len(effective_qs)
                ):
                chunk_q.put((event_type, data))
        except Exception as exc:
            chunk_q.put(("error", str(exc)))
        finally:
            chunk_q.put(None)  # sentinel

    threading.Thread(target=producer, daemon=True).start()

    loop = asyncio.get_running_loop()

    async def llm_sse():
        while True:
            item = await loop.run_in_executor(None, chunk_q.get)
            if item is None:
                break
            event_type, data = item
            if event_type == "chunk":
                yield f"data: {_json.dumps({'type': 'chunk', 'text': data})}\n\n"
            elif event_type == "done":
                mcq_dict = None
                if data.mcq:
                    mcq_dict = data.mcq.model_dump() if hasattr(data.mcq, "model_dump") else data.mcq.dict()
                out = {
                    "type": "complete",
                    "message": data.message,
                    "current_state": data.current_state,
                    "mcq": mcq_dict,
                    "text_input": data.text_input,
                    "is_complete": data.is_complete,
                    "questions_asked_so_far": data.questions_asked_so_far,
                }
                yield f"data: {_json.dumps(out)}\n\n"
            elif event_type == "error":
                yield f"data: {_json.dumps({'type': 'error', 'message': str(data)})}\n\n"

    return StreamingResponse(
        llm_sse(),
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
