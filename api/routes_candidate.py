"""Candidate Routes — Resume upload, profiling chat, and profile retrieval."""

import asyncio
import json as _json
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, UploadFile, File
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy.orm import Session
from typing import Dict, Any, List, Literal, Optional
from pydantic import BaseModel

from database.session import get_db, SessionLocal
from database.models import User, Candidate, Lead, LeadScore
from services.candidate_intelligence.parser import parse_resume
from api.dependencies import get_current_user
from core.analytics import capture, identify

import hashlib
import logging
import time
from datetime import datetime
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/candidate", tags=["Candidate"])


class ChatRequest(BaseModel):
    message: str
    chat_history: List[Dict[str, str]] = []


@router.post("/upload")
async def upload_resume(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload and parse a resume. Returns raw text and metadata preview."""
    contents = await file.read()
    try:
        raw_text, preview = parse_resume(contents, file.filename)

        # Refuse a resume we could not read, instead of reporting success.
        #
        # parse_resume returns empty text for a scanned image PDF, a corrupt
        # file, or a format it cannot handle. That used to create a candidate
        # anyway and return "success", so the student walked into the quiz with
        # no resume behind it: the background profile extraction had nothing to
        # work with (3.6% of uploads never get a resume_profile), the adaptive
        # role options fell back to generic ones, and nothing ever told them.
        # Failing here lets them upload a readable file while they are still on
        # the upload screen and expecting to deal with it.
        if not raw_text or not raw_text.strip():
            logger.warning(
                "[UPLOAD] Unreadable resume from user %s (%s, %d bytes)",
                current_user.id, file.filename, len(contents),
            )
            raise HTTPException(
                status_code=422,
                detail=(
                    "We could not read any text from that file. If it is a "
                    "scanned copy or an image, please upload a text-based PDF "
                    "or a Word document instead."
                ),
            )

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

        capture("resume_uploaded", str(current_user.id), {
            "candidate_id": new_candidate.id,
            "file_type": (file.filename or "").rsplit(".", 1)[-1].lower(),
        })

        # Funnel: create / advance the user's OutreachOrder to stage 1.
        # This is the entry point to the funnel — every uploaded resume
        # produces an order row so we can see drop-off from here on.
        from services.stage_tracking import safe_mark_stage
        safe_mark_stage(db, str(current_user.id), "resume_uploaded",
                        candidate_id=new_candidate.id)

        return {
            "status": "success",
            "candidate_id": new_candidate.id,
            "preview": preview,
        }
    except HTTPException:
        # Already a deliberate, user-facing failure with its own status code —
        # re-raise it rather than flattening it into a generic 400.
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# DEPRECATED — superseded by /chat/stream, which is what the quiz actually uses.
# No caller exists in the frontend, this service, or the extensions. It is left
# mounted rather than deleted so an unknown client gets a logged warning instead
# of a silent 404; once the log shows no hits for a release, delete it along with
# /chat/v2 and the engine helpers only they reach.
@router.post("/{candidate_id}/chat", deprecated=True)
async def candidate_chat(
    candidate_id: int,
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """DEPRECATED. Send a message to the profiling agent and get the next response."""
    t_start = time.perf_counter()
    logger.warning(
        "[DEPRECATED] POST /candidate/%s/chat called by user %s — this endpoint has "
        "no known caller and is scheduled for deletion; use /chat/stream",
        candidate_id, current_user.id,
    )

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


# DEPRECATED — see the note on /chat above. Same story: superseded by
# /chat/stream, no known caller.
@router.post("/{candidate_id}/chat/v2", deprecated=True)
async def candidate_chat_fast(
    candidate_id: int,
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """DEPRECATED. Fast profiling chat using pre-defined static questions."""
    t_start = time.perf_counter()
    logger.warning(
        "[DEPRECATED] POST /candidate/%s/chat/v2 called by user %s — this endpoint "
        "has no known caller and is scheduled for deletion; use /chat/stream",
        candidate_id, current_user.id,
    )

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
    # Drop a user message that repeats the one before it with no question in
    # between.
    #
    # Answers are assigned by position during this replay, so a single duplicate
    # shifts every later answer onto the wrong question key: the student's city
    # is stored as their company stage, and nothing errors. A failed-and-retried
    # turn used to leave exactly such a duplicate behind (the frontend now
    # removes the optimistic message, but older clients are still out there),
    # and the stream fetch's new automatic retry is a second way to produce one.
    #
    # The assistant's question is what separates two answers. Two identical
    # answers to *different* questions are legitimate and common — "Skip" to one
    # question and "Skip" to the next — and those have a question between them,
    # so they are kept. Only a repeat with nothing in between is a duplicate of
    # one answer, which is the bug.
    _SENTINELS = ("__start__", "__resume__", "__generate__")
    raw_user_msgs: list[str] = []
    saw_question_since_last_answer = True
    for m in request.chat_history:
        if m["role"] != "user":
            saw_question_since_last_answer = True
            continue
        content = m["content"]
        if content in _SENTINELS:
            continue
        if (
            raw_user_msgs
            and content == raw_user_msgs[-1]
            and not saw_question_since_last_answer
        ):
            logger.info(
                "[STREAM] Dropping duplicate answer (no question between) for "
                "candidate %s: %.40r", candidate_id, content,
            )
            continue
        raw_user_msgs.append(content)
        saw_question_since_last_answer = False

    # Build answers dict incrementally (needed because sequence depends on answers)
    answers: dict[str, str] = {}
    resume_text = candidate.resume_text or ""
    parsed_json = candidate.parsed_json if isinstance(candidate.parsed_json, dict) else {}

    # Freeze resume_profile for the lifetime of this quiz session.
    # Background LLM extraction writes to candidates.resume_profile asynchronously
    # after upload. Without snapshotting, each question re-queries the DB and can
    # read a different archetype mid-quiz (e.g. Q5 reads "founding product engineer",
    # Q8 reads "zero-to-one gtm generalist" after the extraction overwrites it).
    # Fix: on the first call that has a non-empty profile, store a snapshot in
    # parsed_json["_qps"] and use it exclusively for all subsequent questions.
    # IMPORTANT: only trust the snapshot if it has real data (likely_roles or domain).
    # A snapshot taken before background LLM completed will have these as None —
    # in that case, re-read resume_profile and re-freeze if it's now ready.
    _QPS = "_qps"
    _qps_candidate = parsed_json.get(_QPS)
    _qps_useful = (
        isinstance(_qps_candidate, dict)
        and (_qps_candidate.get("likely_roles") or _qps_candidate.get("domain"))
    )
    if _qps_useful:
        resume_profile = _qps_candidate
    else:
        resume_profile = candidate.resume_profile if isinstance(candidate.resume_profile, dict) else {}
        if resume_profile.get("likely_roles") or resume_profile.get("domain"):
            # LLM extraction is done — lock this in for all future quiz calls.
            try:
                candidate.parsed_json = {**parsed_json, _QPS: resume_profile}
                db.commit()
                parsed_json = candidate.parsed_json
            except Exception as snap_err:
                logger.warning("[STREAM] Could not snapshot resume_profile: %s", snap_err)
                try:
                    db.rollback()
                except Exception:
                    pass

    def _make_state() -> dict:
        return {
            "answers": answers,
            "resume_profile": resume_profile,
            "resume_text": resume_text,
            # _resume_text is a private key used by question_engine for NLP role detection
            "parsed_json": {**parsed_json, "_resume_text": resume_text},
        }

    for answer in raw_user_msgs:
        seq = build_question_sequence(_make_state())
        answered_count = len(answers)
        if answered_count < len(seq):
            answers[seq[answered_count]["key"]] = answer

    # Build state for the *current* turn (current message not yet in answers)
    state = _make_state()
    sequence = build_question_sequence(state)
    q_index = len(answers)  # index of next question to serve

    # Persist the answers we just replayed, keyed by question key, before we
    # serve anything. Until this existed the only write was in the completion
    # branch below, so a user who abandoned at Q5 left nothing behind: 1,559
    # abandoned quizzes stored zero answers and per-question drop-off was not
    # measurable. Writing every turn also gives the server an authoritative copy
    # to resume from, instead of trusting the client's replay to be the only one.
    #
    # Merge rather than replace: a shorter replay (a client that lost history,
    # or a retry that dropped a turn) must not erase keys the server already
    # holds. The replay is still the source of truth for keys it does carry.
    is_first_answer = False
    if answers:
        try:
            stored = candidate.quiz_answers if isinstance(candidate.quiz_answers, dict) else {}
            merged = {**stored, **answers}
            if merged != stored:
                is_first_answer = not stored
                candidate.quiz_answers = merged
                candidate.quiz_answers_updated_at = datetime.utcnow()
                db.commit()
        except Exception as persist_err:
            # A failed answer write must never cost the user their quiz turn —
            # the replay path still works without it, exactly as it did before.
            logger.warning(
                "[STREAM] Could not persist quiz_answers for candidate %s: %s",
                candidate_id, persist_err,
            )
            try:
                db.rollback()
            except Exception:
                pass

    # Funnel: mark "quiz_started" on the user's order the first time an answer
    # is actually stored.
    #
    # This used to fire on `q_index == 0 and (message == "__start__" or no user
    # messages)`, which is unreachable: the frontend serves Q1 from a local
    # constant (Q1_STATIC) and only calls this endpoint once the user has
    # answered it, so the first request always arrives carrying one user message
    # and no client anywhere sends "__start__". Hence quiz_started_at was set on
    # 1 of 4,791 orders. Keying off the first persisted answer measures the same
    # intent ("this user began answering") and needs no frontend change.
    if is_first_answer:
        from services.stage_tracking import safe_mark_stage
        safe_mark_stage(db, str(current_user.id), "quiz_started",
                        candidate_id=candidate_id)

    # ── Quiz complete ──────────────────────────────────────────────────
    if q_index >= len(sequence):
        # Persist dream companies from quiz answers
        # Free text, so it is validated rather than just comma-split. The old
        # split turned "I'm not sure, maybe Google" into two target employers
        # and sent both to lead discovery; 19.6% of candidates had prose stored
        # as company names.
        from services.candidate_intelligence.payload_builder import parse_dream_companies
        candidate.dream_companies = parse_dream_companies(answers.get("dream_companies", ""))
        if candidate.dream_companies:
            logger.info(f"[STREAM] Stored dream_companies={candidate.dream_companies} for candidate {candidate_id}")

        # Flex notes are NOT collected here. build_question_sequence does not
        # add flex_best_project or flex_outcome, so `answers` can never contain
        # them and this branch is unreachable from the quiz.
        #
        # They are collected by the debrief form (/outreach/connect/debrief),
        # which now runs BEFORE the Gmail gate rather than after it. It sat
        # behind that gate, and only 151 of 4,791 orders ever reached
        # gmail_connected, which is why flex_notes coverage fell from 74% to
        # 1.4%. The debrief writes through PUT /candidate/{id}/flex.
        #
        # The read is kept only so a client that still sends these keys is
        # honoured; the comment that used to sit here claimed the quiz collected
        # them, which read as live code and was not.
        best_project = (answers.get("flex_best_project") or "").strip()
        outcome = (answers.get("flex_outcome") or "").strip()
        if best_project or outcome:
            candidate.flex_notes = {**(candidate.flex_notes or {}),
                                    "best_project": best_project, "outcome": outcome}
            logger.info(f"[STREAM] Stored flex_notes from quiz for candidate {candidate_id}")

        db.commit()

        capture("profile_quiz_completed", str(current_user.id), {
            "candidate_id": candidate_id,
            "questions_answered": q_index,
            "has_dream_companies": bool(candidate.dream_companies),
            "has_flex_notes": bool(candidate.flex_notes),
        })

        # Funnel: mark stage 3.
        #
        # This fires when the student finishes answering, which is genuinely
        # what "quiz completed" means, and deliberately still does — the 138
        # orders marked completed with no target_roles were caused by the
        # profile write that follows being fire-and-forget, not by this line.
        # generate-payload is now inline and returns a real status, so a failed
        # build is surfaced to the student instead of leaving the funnel
        # claiming a completion that produced no targeting.
        from services.stage_tracking import safe_mark_stage
        safe_mark_stage(db, str(current_user.id), "quiz_completed",
                        candidate_id=candidate_id)

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
    # build_message renders LLM-derived copy out of resume_profile, so it is the
    # one part of this endpoint that can raise on unexpected data. This route is
    # the only candidate route with no exception handler; without one, a raise
    # here returns a bare 500 with no SSE frame at all, and the frontend — which
    # has no timeout and no retry on this fetch — sits on a spinner forever.
    # Falling back to the unadorned question keeps the quiz moving.
    q_def = sequence[q_index]
    prev_key = sequence[q_index - 1]["key"] if q_index > 0 else None
    is_first = (request.message == "__start__" or q_index == 0)
    prev_answer = answers.get(prev_key) if prev_key else None
    try:
        msg_text = build_message(q_def, prev_key, is_first, prev_answer=prev_answer, resume_profile=resume_profile)
    except Exception as msg_err:
        logger.exception(
            "[STREAM] build_message failed for candidate %s at q_index=%s (%s): %s",
            candidate_id, q_index, q_def.get("key"), msg_err,
        )
        msg_text = q_def.get("message") or ""
    mcq = q_def.get("mcq")

    payload = {
        "type": "complete",
        "message": msg_text,
        "current_state": "MCQ" if mcq else "TEXT",
        "mcq": mcq,
        "text_input": q_def.get("text_input", False),
        "input_placeholder": q_def.get("input_placeholder") or None,
        "is_complete": False,
        "questions_asked_so_far": q_index + 1,
        # The sequence length is already known here and was only ever logged.
        # Sending it lets the quiz show "3 of 9" instead of a progress bar
        # against a hardcoded guess of 10, which is wrong for most students
        # because the sequence is clarity-gated and runs 8 to 11 questions.
        "questions_total": len(sequence),
    }
    logger.info(f"[STREAM] Q{q_index + 1}/{len(sequence)} ({q_def['key']}) served instantly for candidate {candidate_id}")

    async def static_sse():
        yield f"data: {_json.dumps(payload)}\n\n"

    return StreamingResponse(
        static_sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _apply_payload(candidate: Candidate, payload_dict: dict) -> None:
    """Write a built payload onto the candidate row."""
    candidate.parsed_json = payload_dict
    recommended = payload_dict.get("career_analysis", {}).get("recommended_roles", [])
    if recommended:
        candidate.target_roles = [r["title"] for r in recommended]

    # No truthy gate on industries.
    #
    # `if industry_interests:` meant an empty result could never correct a
    # previous one, which is what made the industries race permanent: the first
    # build runs before background resume extraction has landed, derives nothing,
    # and the write is skipped — but so is every later write that would have
    # fixed it, because the value is only ever assigned when non-empty. Assigning
    # unconditionally lets a re-run repair an earlier miss.
    candidate.target_industries = (
        payload_dict.get("preferences", {}).get("industry_interests", []) or []
    )


def _generate_payload_now(db: Session, candidate: Candidate, chat_history_dicts: list[dict]) -> dict:
    """Build the profile payload and store it. Raises on failure."""
    from services.candidate_intelligence.payload_builder import (
        reconstruct_answers,
        build_payload_from_answers,
    )

    answers = reconstruct_answers(chat_history_dicts, candidate)
    logger.info(
        "[PAYLOAD] Reconstructed %d answers: %s", len(answers), list(answers.keys())
    )

    payload_dict = build_payload_from_answers(
        answers=answers,
        candidate=candidate,
        resume_uploaded=bool(candidate.resume_text),
    )
    _apply_payload(candidate, payload_dict)
    db.commit()
    return payload_dict


def _run_generate_payload_background(candidate_id: int, chat_history_dicts: list[dict]) -> None:
    """
    Background worker: generate final payload and store it.
    Opens its own DB session (request session is already closed).
    Uses deterministic parser — zero LLM calls, completes in <50ms.
    """
    import traceback
    t_start = time.perf_counter()
    try:
        db = SessionLocal()
        try:
            from services.candidate_intelligence.payload_builder import (
                reconstruct_answers,
                build_payload_from_answers,
            )

            candidate = db.query(Candidate).filter_by(id=candidate_id).first()
            if not candidate:
                logger.error(f"[PAYLOAD-BG] Candidate {candidate_id} not found")
                return

            # Reconstruct structured answers from chat history (same logic as chat/stream endpoint)
            answers = reconstruct_answers(chat_history_dicts, candidate)
            logger.info(f"[PAYLOAD-BG] Reconstructed {len(answers)} answers: {list(answers.keys())}")

            # Build payload deterministically — no LLM
            payload_dict = build_payload_from_answers(
                answers=answers,
                candidate=candidate,
                resume_uploaded=bool(candidate.resume_text),
            )

            _apply_payload(candidate, payload_dict)
            db.commit()

            logger.info(
                f"[PAYLOAD-BG] Done for candidate {candidate_id} in {(time.perf_counter() - t_start)*1000:.0f}ms "
                f"(deterministic, no LLM)"
            )
        finally:
            db.close()
    except Exception as exc:
        logger.error(f"[PAYLOAD-BG] FAILED for candidate {candidate_id}: {type(exc).__name__}: {exc}")
        logger.error(traceback.format_exc())


@router.post("/{candidate_id}/generate-payload")
async def generate_payload(
    candidate_id: int,
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Build the profile and store it, inline.

    This used to queue a background task and return {"status": "processing"}
    immediately, which meant the response said nothing about whether the write
    succeeded: a failure in the worker was logged and swallowed, the funnel
    still recorded a completed quiz, and the student reached a profile page with
    no targeting behind it. 138 orders are marked quiz_completed with no
    target_roles on their candidate.

    There was never a latency reason for it to be deferred. The build is
    deterministic with zero LLM calls and completes in under 50ms, so it runs
    here and the status code reports what actually happened. profile-status
    still exists for clients that poll.
    """
    candidate = db.query(Candidate).filter_by(id=candidate_id, user_id=current_user.id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    t_start = time.perf_counter()
    try:
        _generate_payload_now(db, candidate, request.chat_history)
    except Exception as exc:
        logger.exception(
            "[PAYLOAD] FAILED for candidate %s: %s: %s",
            candidate_id, type(exc).__name__, exc,
        )
        raise HTTPException(
            status_code=500,
            detail="Could not build your profile. Please try again.",
        )

    logger.info(
        "[PAYLOAD] Done for candidate %s in %.0fms (deterministic, no LLM)",
        candidate_id, (time.perf_counter() - t_start) * 1000,
    )
    return {"status": "ready", "candidate_id": candidate_id}


@router.get("/{candidate_id}/profile-status")
async def get_profile_status(
    candidate_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Poll endpoint — returns ready=true once target_roles is populated.
    Frontend polls this after calling /generate-payload.
    """
    candidate = db.query(Candidate).filter_by(id=candidate_id, user_id=current_user.id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    ready = bool(candidate.target_roles)
    return {"ready": ready}


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

    from services.candidate_intelligence.payload_builder import compute_hiring_manager_titles
    resume_profile = candidate.resume_profile or {}
    target_roles = candidate.target_roles or []
    hiring_manager_titles = compute_hiring_manager_titles(target_roles, resume_profile)
    return {
        "candidate_id": candidate.id,
        "parsed_json": candidate.parsed_json,
        "resume_profile": resume_profile,
        "target_roles": target_roles,
        "target_industries": candidate.target_industries,
        "dream_companies": candidate.dream_companies,
        "created_at": candidate.created_at.isoformat() if candidate.created_at else None,
        "hiring_manager_titles": hiring_manager_titles,
    }


# Plain `def`: this is the product's heaviest poll (hundreds of rows, all
# blocking SQLAlchemy), so it runs in the threadpool, not on the event loop.
@router.get("/latest")
def get_latest_candidate(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The caller's most recent candidate, for pages that lost the id client-side.

    candidateId otherwise lives only in the browser's localStorage, so a cleared
    store or a new device sent the user back to resume upload even though their
    leads were already here.
    """
    candidate = (
        db.query(Candidate.id)
        .filter(Candidate.user_id == current_user.id)
        .order_by(Candidate.id.desc())
        .first()
    )
    return {"candidate_id": candidate.id if candidate else None}


@router.get("/{candidate_id}/leads")
def get_candidate_leads(
    request: Request,
    candidate_id: int,
    limit: Optional[int] = Query(None, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    fields: Optional[Literal["justification"]] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get a candidate's leads with their scores, best first.

    With no query params this returns every lead, as it always has. `limit` /
    `offset` page through the same ranked list, and `total` is always the full
    count before paging. `fields=justification` is the cheap poll: each item is
    just {id, score: {overall, justification}} so a client that already holds
    the lead cards can pick up bullets as they stream in.
    """
    logger.info(f"[LeadSearch] GET /candidate/{candidate_id}/leads — user_id={current_user.id}")

    candidate = db.query(Candidate).filter_by(
        id=candidate_id, user_id=current_user.id
    ).first()
    if not candidate:
        logger.warning(f"[LeadSearch] Candidate {candidate_id} not found for user {current_user.id}")
        raise HTTPException(status_code=404, detail="Candidate not found")

    light = fields == "justification"

    # Ordered, so identical polls return identical lists. Heap order moves as
    # the justification pass rewrites company_domain on rows mid-poll.
    lead_query = db.query(Lead.id) if light else db.query(Lead)
    leads = lead_query.filter(Lead.candidate_id == candidate_id).order_by(Lead.id).all()
    logger.info(f"[LeadSearch] Leads retrieved from DB: {len(leads)}")

    # Batch-fetch all LeadScores for these leads in one query (was N+1: 1 + len(leads)).
    # Ordered by id so that if a lead ever has two score rows, the newest wins every time.
    lead_ids = [l.id for l in leads]
    scores_by_lead: dict[int, LeadScore] = {}
    if lead_ids:
        score_query = (
            db.query(LeadScore.lead_id, LeadScore.overall_score, LeadScore.justification_json)
            if light else db.query(LeadScore)
        )
        for s in score_query.filter(LeadScore.lead_id.in_(lead_ids)).order_by(LeadScore.id).all():
            scores_by_lead[s.lead_id] = s

    # No score floor: users pay for ~500 emails so they see every lead, and
    # quality comes from the sort order. Unscored leads (discovery may still be
    # running for them) are included too.
    results = []
    for lead in leads:
        score = scores_by_lead.get(lead.id)
        if light:
            results.append({
                "id": lead.id,
                "score": {
                    "overall": score.overall_score,
                    "justification": score.justification_json,
                } if score else None,
            })
            continue
        # `explanation` and the five *_relevance ints are no longer sent: no
        # client reads them (frontend, admin panel and extension all checked),
        # and explanation was one per-run string repeated on every lead.
        results.append({
            "id": lead.id,
            "name": lead.name,
            "title": lead.title,
            "company": lead.company,
            "company_domain": lead.company_domain,
            "industry": lead.industry,
            "location": lead.location,
            "linkedin_url": lead.linkedin_url,
            "email": lead.email,
            "email_verified": lead.email_verified,
            "company_size": lead.company_size,
            "status": lead.status,
            "score": {
                "overall": score.overall_score,
                "justification": score.justification_json,
            } if score else None,
        })

    # Sort by score descending, lead id ascending as a total-order tiebreak
    results.sort(key=lambda x: (-(x["score"]["overall"] if x["score"] else 0), x["id"]))

    total = len(results)
    if offset or limit is not None:
        results = results[offset: offset + limit if limit is not None else None]

    logger.info(f"[LeadSearch] Returning {len(results)}/{total} leads to frontend (scored: {sum(1 for r in results if r['score'])})")

    # ETag over the exact body. The results page polls this every 15s while
    # bullets stream in; once nothing has changed, a poll costs a 304 and no
    # payload instead of the full list again.
    body = jsonable_encoder({"leads": results, "total": total})
    etag = '"' + hashlib.sha256(_json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32] + '"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if_none_match = request.headers.get("if-none-match", "")
    if etag in [t.strip() for t in if_none_match.split(",")]:
        return Response(status_code=304, headers=headers)
    return JSONResponse(body, headers=headers)


class FlexNotesRequest(BaseModel):
    best_project: str = ""
    outcome: str = ""
    why_now: str = ""
    # The LinkedIn onboarding form (linkedin.onboarding.profile) sends the
    # user's typed target role + location. It wraps them in a nested
    # `flex_notes` object, so accept that shape too. These drive lead discovery
    # and must take priority over résumé-derived targeting.
    target_role_user_input: Optional[str] = None
    location_user_input: Optional[str] = None
    flex_notes: Optional[Dict[str, Any]] = None


@router.put("/{candidate_id}/flex")
@router.patch("/{candidate_id}/flex-notes")
async def save_flex_notes(
    candidate_id: int,
    request: FlexNotesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save flex notes. Two callers share this:
      - the quiz/debrief sends best_project/outcome/why_now (email personalisation)
      - the LinkedIn onboarding sends target_role_user_input/location_user_input
    so we MERGE into flex_notes rather than clobber the other caller's fields."""
    candidate = db.query(Candidate).filter(
        Candidate.id == candidate_id,
        Candidate.user_id == current_user.id,
    ).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    flex = dict(candidate.flex_notes or {})
    # Debrief fields — only write when the debrief actually supplied content.
    if request.best_project or request.outcome or request.why_now:
        flex["best_project"] = request.best_project.strip()
        flex["outcome"] = request.outcome.strip()
        flex["why_now"] = request.why_now.strip()

    # LinkedIn onboarding targeting — accept either flat fields or the nested
    # `flex_notes` object the onboarding form actually sends.
    nested = request.flex_notes if isinstance(request.flex_notes, dict) else {}
    role_in = request.target_role_user_input
    if role_in is None:
        role_in = nested.get("target_role_user_input")
    loc_in = request.location_user_input
    if loc_in is None:
        loc_in = nested.get("location_user_input")
    if role_in is not None:
        flex["target_role_user_input"] = (role_in or "").strip()
    if loc_in is not None:
        flex["location_user_input"] = (loc_in or "").strip()
    for k in ("company_stage_user_input", "source"):
        if k in nested:
            flex[k] = nested[k]

    candidate.flex_notes = flex
    db.commit()
    return {"ok": True}
