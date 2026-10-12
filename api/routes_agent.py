"""Student agent HTTP API, Phase 1 (contract section 3). Browser base: /api/v1/outreach/agent.

The route layer only gates, parses, commits and shapes. Reads go through
`services.agent.reads`, every workspace mutation through the same
`services.agent.actions` function the matching tool calls (invariant 4), and
the chat turn through `services.agent.runtime` on a worker thread.

Phase 1 invariants that live here: every route except bootstrap is gated
first (kill switch, flag, allowlist); nothing here sends, spends or writes
emails_sent, campaigns, orders or credits; no lead address or phone is in any
body (the schemas forbid unknown fields, and `has_address` is all they carry).

Handlers are plain `def` (blocking SQLAlchemy runs on the threadpool). The
stream handler commits the user message and the run before returning, because
the injected session is closed before the stream body runs.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.dependencies import get_current_user
from core.logger import get_logger
from database.models import AgentMessage, AgentRun, User
from database.session import get_db
from services.agent import actions, confirmations, context, events, gating, llm, reads, runtime, store
from services.agent.prompt import PROMPT_VERSION
from services.agent.schemas import (
    STATUS_LABELS_PHASE1, ApproveAll, ApproveDraft, CreateDrafts, CreateMemory, CreateThread, DiscardDraft,
    EditDraft, ReviseDraft, SendMessage, SkipMatch, UpdateNotes, UpdateSettings,
)
from services.agent.schemas_chat import (
    BootUser, Bootstrap, BootstrapDisabled, CancelResult, OkResult, RunPoll, ThreadDetail, ThreadList,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/agent", tags=["agent"])

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
PING_SECONDS = 15


# ---------------------------------------------------------------- helpers

def _out(model: Any, status_code: int = 200) -> JSONResponse:
    """Every field present, null when empty (contract section 2)."""
    return JSONResponse(content=model.model_dump(mode="json"), status_code=status_code)


def _gate(user: User) -> str:
    gating.require_enabled(user)
    return str(user.id)


def _not_found() -> HTTPException:
    return gating.agent_error("not_found")


def _outreach_id(raw: str) -> str:
    """A malformed outreach id is a 404, never a 422."""
    if context.parse_outreach_id(raw) is None:
        raise _not_found()
    return raw


def _version_conflict(db: Session, user_id: str, row: Any) -> HTTPException:
    current = None
    try:
        current = reads.outreach_detail(db, user_id, f"d_{row.id}")
    except Exception:
        logger.warning("[AGENT] could not load the current draft for a version conflict")
    return gating.agent_error("version_conflict", "This draft changed. Here is the latest version.",
                              current=current)


def _run(db: Session, user_id: str, fn: Callable[[], Any], commit: bool = True) -> Any:
    """Call a read or action, commit, and turn its errors into the contract's codes."""
    try:
        result = fn()
        if commit:
            db.commit()
        return result
    except HTTPException:
        db.rollback()
        raise
    except context.ActionError as e:
        db.rollback()
        raise gating.agent_error(e.code, e.message, getattr(e, "status", None),
                                 **(getattr(e, "extra", None) or {})) from None
    except store.NotFound:
        db.rollback()
        raise _not_found() from None
    except store.VersionConflict as e:
        db.rollback()
        raise _version_conflict(db, user_id, e.current) from None
    except store.WrongState:
        db.rollback()
        raise gating.agent_error("wrong_state") from None
    except Exception as e:
        db.rollback()
        logger.exception("[AGENT] route failed: %s", type(e).__name__)
        raise HTTPException(status_code=500,
                            detail={"code": "internal", "message": gating.SERVER_ERROR}) from None


def _latest_run(db: Session, thread_id: int, user_id: str) -> Optional[Any]:
    row = (db.query(AgentRun).filter(AgentRun.thread_id == thread_id, AgentRun.user_id == user_id)
           .order_by(AgentRun.id.desc()).first())
    return store.run_out(row) if row is not None else None


def _thread_detail(db: Session, thread: Any, user_id: str, before: Optional[int] = None,
                   limit: int = 50) -> ThreadDetail:
    store.reap_stale_runs(db, thread_id=thread.id)
    messages, has_more = store.display_messages(db, thread.id, user_id, before, limit)
    return ThreadDetail(thread=store.thread_out(thread), messages=messages,
                        latest_run=_latest_run(db, thread.id, user_id), has_more=has_more)


# ---------------------------------------------------------------- 1, 2: bootstrap and status

@router.get("/bootstrap")
def bootstrap(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    reason = gating.gate_reason(current_user)
    if reason:
        return _out(BootstrapDisabled(reason=reason))
    uid = str(current_user.id)

    def build() -> Bootstrap:
        cand = context.resolve_agent_candidate(db, uid)
        mains = store.list_threads(db, uid, scope="main", limit=1)
        thread = mains[0] if mains else store.create_thread(
            db, uid, scope="main", candidate_id=cand.id if cand is not None else None)
        status = reads.status(db, uid)
        has_candidate = cand is not None
        return Bootstrap(
            user=BootUser(id=uid, first_name=context.first_name_of(current_user.name) or status.first_name),
            status=status,
            settings=reads.settings(db, uid),
            status_labels=dict(STATUS_LABELS_PHASE1),
            top_matches=reads.top_matches(db, uid) if has_candidate else [],
            outreach=reads.outreach_list(db, uid, status="all", limit=20, offset=0),
            threads=[store.thread_out(t) for t in store.list_threads(db, uid, limit=20)],
            thread=_thread_detail(db, thread, uid),
            suggestions=reads.default_suggestions(db, uid, st=status),
            greeting=reads.greeting(db, uid, st=status),
        )

    return _out(_run(db, uid, build))


@router.get("/status")
def get_status(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    uid = _gate(current_user)
    return _out(_run(db, uid, lambda: reads.status(db, uid), commit=False))


# ---------------------------------------------------------------- 3 to 5: matches

@router.get("/matches")
def list_matches(
    q: Optional[str] = Query(None, max_length=120),
    company: Optional[str] = Query(None, max_length=120),
    city: Optional[str] = Query(None, max_length=120),
    title: Optional[str] = Query(None, max_length=120),
    min_score: Optional[float] = Query(None, ge=0, le=100),
    state: Optional[str] = Query(None, pattern="^(new|drafted|contacted|skipped)$"),
    strong_only: bool = False,
    exclude_contacted: bool = False,
    limit: int = Query(25, ge=1, le=50),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = _gate(current_user)
    return _out(_run(db, uid, lambda: reads.ranked_matches(
        db, uid, q=q, company=company, city=city, title=title, min_score=min_score, state=state,
        strong_only=strong_only, exclude_contacted=exclude_contacted, limit=limit, offset=offset), commit=False))


@router.get("/matches/{lead_id}")
def get_match(lead_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    uid = _gate(current_user)
    return _out(_run(db, uid, lambda: reads.match_detail(db, uid, lead_id), commit=False))


@router.post("/matches/{lead_id}/skip")
def skip_match(lead_id: int, body: SkipMatch, current_user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    uid = _gate(current_user)
    return _out(_run(db, uid, lambda: actions.skip_lead(db, uid, lead_id, reason=body.reason)))


# ---------------------------------------------------------------- 6 to 14: outreach

@router.get("/outreach")
def list_outreach(
    status: str = Query("all", pattern="^(all|needs_you|scheduled|sent|replied|failed|skipped)$"),
    lead_id: Optional[int] = None,
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = _gate(current_user)
    # Reads reap stale drafting rows, so this one commits.
    return _out(_run(db, uid, lambda: reads.outreach_list(db, uid, status=status, lead_id=lead_id,
                                                         limit=limit, offset=offset)))


# approve-all is declared before /outreach/{outreach_id}/... so the literal path wins.
@router.post("/outreach/approve-all")
def approve_all(body: ApproveAll, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    uid = _gate(current_user)
    return _out(_run(db, uid, lambda: actions.approve_all(db, uid, body.ids)))


@router.post("/outreach/drafts", status_code=202)
def create_drafts(body: CreateDrafts, current_user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    uid = _gate(current_user)
    result = _run(db, uid, lambda: actions.start_drafts(db, uid, body.lead_ids, style=body.style,
                                                        instructions=body.instructions))
    actions.submit_drafts(result)
    return _out(result, status_code=202)


@router.get("/outreach/{outreach_id}")
def get_outreach(outreach_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    uid = _gate(current_user)
    oid = _outreach_id(outreach_id)
    return _out(_run(db, uid, lambda: reads.outreach_detail(db, uid, oid)))


@router.post("/outreach/{outreach_id}/revise")
def revise(outreach_id: str, body: ReviseDraft, current_user: User = Depends(get_current_user),
           db: Session = Depends(get_db)):
    uid = _gate(current_user)
    oid = _outreach_id(outreach_id)
    # A rewrite waits on the writer, so it takes a pod run slot like a turn (503 busy when none).
    slot = gating.acquire_slot()
    try:
        return _out(_run(db, uid, lambda: actions.revise(db, uid, oid, body.instruction,
                                                         expected_version=body.expected_version)))
    finally:
        slot.release()


@router.patch("/outreach/{outreach_id}")
def edit(outreach_id: str, body: EditDraft, current_user: User = Depends(get_current_user),
         db: Session = Depends(get_db)):
    uid = _gate(current_user)
    oid = _outreach_id(outreach_id)
    return _out(_run(db, uid, lambda: actions.edit(db, uid, oid, subject=body.subject, body=body.body,
                                                   expected_version=body.expected_version)))


@router.post("/outreach/{outreach_id}/approve")
def approve(outreach_id: str, body: ApproveDraft, current_user: User = Depends(get_current_user),
            db: Session = Depends(get_db)):
    uid = _gate(current_user)
    oid = _outreach_id(outreach_id)
    return _out(_run(db, uid, lambda: actions.approve(db, uid, oid, expected_version=body.expected_version)))


@router.post("/outreach/{outreach_id}/discard")
def discard(outreach_id: str, body: DiscardDraft, current_user: User = Depends(get_current_user),
            db: Session = Depends(get_db)):
    uid = _gate(current_user)
    oid = _outreach_id(outreach_id)
    return _out(_run(db, uid, lambda: actions.discard(db, uid, oid, reason=body.reason)))


@router.post("/outreach/{outreach_id}/restore")
def restore(outreach_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    uid = _gate(current_user)
    oid = _outreach_id(outreach_id)
    _run(db, uid, lambda: actions.restore(db, uid, oid))
    return _out(OkResult())


# ---------------------------------------------------------------- 15 to 22: inbox, profile, memories, settings

@router.get("/replies")
def list_replies(
    group: Optional[str] = Query(None, pattern="^(positive|question|not_now|bounced)$"),
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = _gate(current_user)
    return _out(_run(db, uid, lambda: reads.replies(db, uid, group=group, limit=limit, offset=offset),
                     commit=False))


@router.get("/profile")
def get_profile(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    uid = _gate(current_user)
    return _out(_run(db, uid, lambda: reads.profile(db, uid), commit=False))


@router.patch("/profile/notes")
def update_notes(body: UpdateNotes, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    uid = _gate(current_user)
    return _out(_run(db, uid, lambda: actions.update_notes(db, uid, body)))


@router.get("/memories")
def list_memories(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    uid = _gate(current_user)
    return _out(_run(db, uid, lambda: reads.memories(db, uid), commit=False))


@router.post("/memories")
def create_memory(body: CreateMemory, current_user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    uid = _gate(current_user)
    return _out(_run(db, uid, lambda: actions.save_memory(
        db, uid, body.text, scope=body.scope, company=body.company, use_in_emails=body.use_in_emails,
        source="settings")))


@router.delete("/memories/{memory_id}")
def delete_memory(memory_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    uid = _gate(current_user)
    _run(db, uid, lambda: actions.delete_memory(db, uid, memory_id))
    return _out(OkResult())


@router.get("/settings")
def get_settings(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    uid = _gate(current_user)
    return _out(_run(db, uid, lambda: reads.settings(db, uid), commit=False))


@router.patch("/settings")
def update_settings(body: UpdateSettings, current_user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    uid = _gate(current_user)
    return _out(_run(db, uid, lambda: actions.update_settings(db, uid, body)))


# ---------------------------------------------------------------- 23 to 26: threads

def _owned_thread(db: Session, thread_id: int, user_id: str) -> Any:
    try:
        thread = store.get_thread(db, thread_id, user_id)
    except store.NotFound:
        raise _not_found() from None
    if thread.status != "active":
        raise _not_found()
    return thread


@router.get("/threads")
def list_threads(scope: Optional[str] = Query(None, max_length=40), current_user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    uid = _gate(current_user)
    rows = _run(db, uid, lambda: store.list_threads(db, uid, scope=scope, limit=20), commit=False)
    return _out(ThreadList(items=[store.thread_out(t) for t in rows]))


@router.post("/threads")
def create_thread(body: CreateThread, current_user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    uid = _gate(current_user)

    def make():
        cand = context.resolve_agent_candidate(db, uid)
        cid = cand.id if cand is not None else None
        if body.scope == "main":
            return store.create_thread(db, uid, scope="main", title=body.title, candidate_id=cid)
        if context.owned_outreach(db, uid, body.scope.split(":", 1)[1]) is None:
            raise _not_found()
        return store.get_or_create_scoped_thread(db, uid, body.scope, title=body.title, candidate_id=cid)

    thread = _run(db, uid, make)
    return _out(store.thread_out(thread))


@router.get("/threads/{thread_id}")
def get_thread(thread_id: int, before: Optional[int] = Query(None, ge=1), limit: int = Query(50, ge=1, le=100),
               current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    uid = _gate(current_user)
    thread = _owned_thread(db, thread_id, uid)
    return _out(_run(db, uid, lambda: _thread_detail(db, thread, uid, before, limit)))


@router.delete("/threads/{thread_id}")
def delete_thread(thread_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    uid = _gate(current_user)
    _owned_thread(db, thread_id, uid)
    _run(db, uid, lambda: store.archive_thread(db, thread_id, uid))
    return _out(OkResult())


# ---------------------------------------------------------------- 27: a turn, streamed

def _prior_turn(db: Session, thread_id: int, client_turn_id: str) -> Optional[int]:
    row = (db.query(AgentMessage.run_id)
           .filter(AgentMessage.thread_id == thread_id, AgentMessage.client_turn_id == client_turn_id).first())
    return None if row is None else (row[0] or 0)


def lock_turns(db: Session, user_id: str) -> None:
    """Serialise turn starts for one user across pods until this transaction
    ends (start_run commits it), so the live-run checks and the insert cannot
    interleave. A transaction-scoped advisory lock on Postgres; SQLite (tests)
    has one connection and needs none."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"agent_turn:{user_id}"})


def _start_worker(run_id: int, q: "queue.Queue", slot: Any, user_message_id: int, confirmation: Any) -> None:
    threading.Thread(target=runtime.run_turn, name=f"agent-run-{run_id}", daemon=True, args=(run_id, q, slot),
                     kwargs={"user_message_id": user_message_id, "confirmation": confirmation}).start()


@router.post("/threads/{thread_id}/messages")
def send_message(thread_id: int, body: SendMessage, current_user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    """Checks, in the contract's order, before any byte is streamed: gate, thread
    ownership, stale-run reap, live run, dedupe, daily caps, pod slot, ticket."""
    uid = _gate(current_user)
    thread = _owned_thread(db, thread_id, uid)
    store.reap_stale_runs(db, user_id=uid)
    lock_turns(db, uid)  # held until start_run commits, or until an error below rolls back
    try:
        gating.check_live_runs(store.live_runs(db, uid), thread.id)
        prior = _prior_turn(db, thread.id, body.client_turn_id)
        if prior is not None:
            raise gating.agent_error("duplicate_turn", run_id=prior)
        gating.check_daily_caps(store.usage_today(db, uid))
    except HTTPException:
        db.rollback()
        raise
    slot = gating.acquire_slot()
    try:
        if body.confirmation is not None:
            confirmations.validate(db, uid, thread.id, body.confirmation)
        cand = context.resolve_agent_candidate(db, uid, thread)
        # Counted in the same transaction as the run, so a failed start leaves neither behind.
        store.bump_usage(db, uid, messages=1)
        run_id, user_message_id = store.start_run(
            db, thread, uid, body, model=llm.deployment(), prompt_version=PROMPT_VERSION,
            candidate_id=cand.id if cand is not None else None)
    except store.DuplicateTurn as e:
        slot.release()
        db.rollback()
        raise gating.agent_error("duplicate_turn", run_id=e.run_id) from None
    except HTTPException:
        slot.release()
        db.rollback()
        raise
    except Exception as e:
        slot.release()
        db.rollback()
        logger.exception("[AGENT] could not start a turn: %s", type(e).__name__)
        raise HTTPException(status_code=500,
                            detail={"code": "internal", "message": gating.SERVER_ERROR}) from None
    logger.info("[AGENT] turn accepted: run %s thread %s", run_id, thread.id, extra={"event": "agent_message_sent"})
    q: queue.Queue = queue.Queue()
    _start_worker(run_id, q, slot, user_message_id, body.confirmation)
    return StreamingResponse(events.drain(q, PING_SECONDS), media_type="text/event-stream", headers=SSE_HEADERS)


# ---------------------------------------------------------------- 28, 29: runs

@router.get("/runs/{run_id}")
def poll_run(run_id: int, after_seq: int = Query(0, ge=0), current_user: User = Depends(get_current_user),
             db: Session = Depends(get_db)):
    uid = _gate(current_user)

    def build() -> RunPoll:
        store.get_run(db, run_id, uid)
        store.reap_stale_runs(db, run_id=run_id, user_id=uid)
        run = store.get_run(db, run_id, uid)
        db.refresh(run)
        frames = events.valid_frames(store.events_after(db, run_id, uid, after_seq))
        message = store.final_message(db, run_id, uid) if run.status != "running" else None
        return RunPoll(run=store.run_out(run), events=frames, message=message)

    return _out(_run(db, uid, build, commit=False))


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    uid = _gate(current_user)
    run = _run(db, uid, lambda: store.request_cancel(db, run_id, uid), commit=False)
    return _out(CancelResult(status=run.status))
