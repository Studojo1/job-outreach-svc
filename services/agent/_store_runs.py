"""Runs, confirmation tickets, memories and settings. Part of services/agent/store.py.

Import these through `services.agent.store`, never from this module directly:
store re-exports every public name here (store.py keeps the shared helpers).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import cast, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.models import AgentConfirmation, AgentMemory, AgentMessage, AgentRun, AgentSetting
from services.agent.schemas import AgentSettings, Memory, NeverContact
from services.agent.schemas_chat import Confirmation, Run
from services.agent.store import (
    CONFIRMATION_TTL_MINUTES, INTERRUPTED_MESSAGE, MEMORY_CAP, MEMORY_SCOPES, MEMORY_SOURCES, STALE_RUN_SECONDS, DuplicateTurn, NotFound, WrongState, _now, _owned, _run_row,
    add_user_message, bump_usage, get_thread, iso, touch_thread,
)


# ---------------------------------------------------------------- runs

def run_out(r: AgentRun) -> Run:
    code = r.error_code if r.error_code in ("internal", "model_unavailable", "content_filter", "interrupted") else None
    return Run(id=r.id, thread_id=r.thread_id, status=r.status, started_at=iso(r.started_at),
               finished_at=iso(r.finished_at), error_code=code if r.status == "error" else None)


def start_run(db: Session, thread, user_id: str, body, *, model: Optional[str] = None,
              prompt_version: Optional[str] = None, candidate_id: Optional[int] = None) -> tuple[int, int]:
    """Insert the run and the user message, then COMMIT. `thread` is a thread row or id; `body` is
    a schemas.SendMessage. Returns (run_id, user_message_id). A repeated client_turn_id on the
    thread raises DuplicateTurn(run_id of the first send). An empty content with a confirmation
    is stored as "Confirm" or "Cancel"."""
    thread_id = thread if isinstance(thread, int) else thread.id
    get_thread(db, thread_id, user_id)
    turn_id = body.client_turn_id
    prior = (db.query(AgentMessage.run_id).filter(AgentMessage.thread_id == thread_id,
                                                  AgentMessage.client_turn_id == turn_id).first())
    if prior is not None:
        raise DuplicateTurn(prior[0])
    conf = getattr(body, "confirmation", None)
    content = body.content or ""
    if not content and conf is not None:
        content = "Confirm" if conf.decision == "approve" else "Cancel"
    pc = getattr(body, "page_context", None)
    meta = {"page_context": pc.model_dump(mode="json") if pc is not None else None}
    if conf is not None:
        meta["confirmation"] = conf.model_dump(mode="json")
    if getattr(body, "time_zone", None):
        meta["time_zone"] = body.time_zone
    now = _now()
    run = AgentRun(thread_id=thread_id, user_id=user_id, status="running", events=[], model=model,
                   prompt_version=prompt_version, confirmation_id=conf.id if conf is not None else None,
                   heartbeat_at=now, started_at=now)
    try:
        db.add(run)
        db.flush()
        msg = add_user_message(db, thread_id, user_id, content, run_id=run.id, client_turn_id=turn_id, meta=meta)
        touch_thread(db, thread_id, user_id, title=content if conf is None else None, candidate_id=candidate_id)
        db.commit()
    except IntegrityError:
        db.rollback()
        prior = (db.query(AgentMessage.run_id).filter(AgentMessage.thread_id == thread_id,
                                                      AgentMessage.client_turn_id == turn_id).first())
        raise DuplicateTurn(prior[0] if prior else None) from None
    return run.id, msg.id


def get_run(db: Session, run_id: int, user_id: str) -> AgentRun:
    return _owned(db.get(AgentRun, run_id), user_id)


def appended_events(frames: list[dict]):
    """`events || frames` as one SQL expression: on Postgres an append is a
    single atomic UPDATE, so the worker and the reaper can never overwrite each
    other's frames, and the list is not read back into Python first."""
    return func.coalesce(AgentRun.events, literal_column("'[]'::jsonb")).op("||", return_type=JSONB)(
        cast([dict(f) for f in frames], JSONB))


def _is_postgres(db: Session) -> bool:
    return db.get_bind().dialect.name == "postgresql"


def append_event(db: Session, run_id: int, event: dict, *, user_id: Optional[str] = None,
                 commit: bool = True) -> None:
    """Persist one stream frame (anything but `text`) so GET /runs/{id}?after_seq= replays it."""
    _run_row(db, run_id, user_id)
    if _is_postgres(db):
        events = appended_events([event])
    else:  # SQLite (tests): read, append, write
        current = db.execute(select(AgentRun.events).where(AgentRun.id == run_id)).scalar_one()
        events = list(current or []) + [dict(event)]
    db.execute(update(AgentRun).where(AgentRun.id == run_id).values(events=events, heartbeat_at=_now()))
    db.commit() if commit else db.flush()


def events_after(db: Session, run_id: int, user_id: str, after_seq: int = 0) -> list[dict]:
    get_run(db, run_id, user_id)
    events = db.execute(select(AgentRun.events).where(AgentRun.id == run_id)).scalar_one() or []
    return sorted((e for e in events if int(e.get("seq") or 0) > after_seq), key=lambda e: int(e.get("seq") or 0))


def _usage_numbers(usage: Any) -> tuple[int, int]:
    if usage is None:
        return 0, 0
    get = usage.get if isinstance(usage, dict) else (lambda k, d=None: getattr(usage, k, d))
    tin = get("tokens_in") or get("prompt_tokens") or get("input_tokens") or 0
    tout = get("tokens_out") or get("completion_tokens") or get("output_tokens") or 0
    return int(tin), int(tout)


def add_usage(db: Session, run_id: int, usage: Any = None, *, tokens_in: int = 0, tokens_out: int = 0,
              steps: int = 0, tool_calls: int = 0, drafts_created: int = 0, user_id: Optional[str] = None,
              commit: bool = True) -> AgentRun:
    """Add to the run's counters. `usage` may be a dict or object with prompt/completion (or
    tokens_in/out) counts. Tokens also go to today's agent_usage_daily for the token cap."""
    run = _run_row(db, run_id, user_id)
    u_in, u_out = _usage_numbers(usage)
    tin, tout = tokens_in + u_in, tokens_out + u_out
    db.execute(update(AgentRun).where(AgentRun.id == run_id).values(
        tokens_in=AgentRun.tokens_in + tin, tokens_out=AgentRun.tokens_out + tout, steps=AgentRun.steps + steps,
        tool_calls=AgentRun.tool_calls + tool_calls, drafts_created=AgentRun.drafts_created + drafts_created,
        heartbeat_at=_now()))
    if tin + tout:
        bump_usage(db, run.user_id, tokens=tin + tout)
    db.commit() if commit else db.flush()
    db.refresh(run)
    return run


def heartbeat(db: Session, run_id: int, *, user_id: Optional[str] = None, commit: bool = True) -> None:
    _run_row(db, run_id, user_id)
    db.execute(update(AgentRun).where(AgentRun.id == run_id).values(heartbeat_at=_now()))
    db.commit() if commit else db.flush()


def request_cancel(db: Session, run_id: int, user_id: str) -> AgentRun:
    """Idempotent. Sets cancel_requested_at on a running run (any pod's worker sees it) and commits."""
    run = get_run(db, run_id, user_id)
    if run.status == "running" and run.cancel_requested_at is None:
        db.execute(update(AgentRun).where(AgentRun.id == run_id, AgentRun.cancel_requested_at.is_(None))
                   .values(cancel_requested_at=_now()))
        db.commit()
        db.refresh(run)
    return run


def cancel_requested(db: Session, run_id: int, *, user_id: Optional[str] = None) -> bool:
    row = db.execute(select(AgentRun.cancel_requested_at, AgentRun.user_id).where(AgentRun.id == run_id)).first()
    if row is None or (user_id is not None and row.user_id != user_id):
        raise NotFound()
    return row.cancel_requested_at is not None


def live_runs(db: Session, user_id: str, *, thread_id: Optional[int] = None) -> list[AgentRun]:
    q = db.query(AgentRun).filter(AgentRun.user_id == user_id, AgentRun.status == "running")
    if thread_id is not None:
        q = q.filter(AgentRun.thread_id == thread_id)
    return q.order_by(AgentRun.id.asc()).all()


def _terminal(db: Session, run_id: int, values: dict, commit: bool) -> bool:
    res = db.execute(update(AgentRun).where(AgentRun.id == run_id, AgentRun.status == "running")
                     .values(finished_at=_now(), heartbeat_at=_now(), **values))
    db.commit() if commit else db.flush()
    return res.rowcount == 1


def finish_run(db: Session, run_id: int, status: str = "done", *, user_id: Optional[str] = None,
               commit: bool = True) -> bool:
    """done | cancelled | limit. Only a running run moves; returns False if it was already terminal
    (for example reaped as interrupted). Commits together with save_final_message's row."""
    if status not in ("done", "cancelled", "limit"):
        raise ValueError(f"finish_run status {status!r}")
    _run_row(db, run_id, user_id)
    return _terminal(db, run_id, {"status": status}, commit)


def fail_run(db: Session, run_id: int, code: str, error: Optional[str] = None, *, user_id: Optional[str] = None,
             commit: bool = True) -> bool:
    """Status error with a StreamErrorCode. `error` is internal (logs and admin), never shown.
    A failed run writes no assistant message."""
    _run_row(db, run_id, user_id)
    return _terminal(db, run_id, {"status": "error", "error_code": code[:30],
                                  "error": (error or "")[:2000] or None}, commit)


def reap_stale_runs(db: Session, thread_id: Optional[int] = None, user_id: Optional[str] = None, *,
                    run_id: Optional[int] = None, now: Optional[datetime] = None,
                    max_age_seconds: int = STALE_RUN_SECONDS) -> list[int]:
    """Running runs whose heartbeat is older than 90 s (the pod died) become error `interrupted`,
    with a persisted `error` frame so a poller ends cleanly. Commits. Returns reaped ids."""
    cutoff = (now or _now()) - timedelta(seconds=max_age_seconds)
    q = (db.query(AgentRun).execution_options(populate_existing=True)
         .filter(AgentRun.status == "running", AgentRun.heartbeat_at < cutoff))
    if thread_id is not None:
        q = q.filter(AgentRun.thread_id == thread_id)
    if user_id is not None:
        q = q.filter(AgentRun.user_id == user_id)
    if run_id is not None:
        q = q.filter(AgentRun.id == run_id)
    reaped = []
    for run in q.all():
        events = list(run.events or [])
        seq = max([int(e.get("seq") or 0) for e in events] or [0]) + 1
        frame = {"type": "error", "seq": seq, "code": "interrupted", "message": INTERRUPTED_MESSAGE,
                 "retryable": True}
        res = db.execute(update(AgentRun).where(AgentRun.id == run.id, AgentRun.status == "running").values(
            status="error", error_code="interrupted", error="heartbeat stale", finished_at=_now(),
            events=appended_events([frame]) if _is_postgres(db) else events + [frame]))
        if res.rowcount == 1:
            reaped.append(run.id)
    if reaped:
        db.commit()
    return reaped



# ---------------------------------------------------------------- confirmation tickets

def confirmation_status(c: AgentConfirmation, now: Optional[datetime] = None) -> str:
    if c.status == "pending" and c.expires_at <= (now or _now()):
        return "expired"
    return c.status


def confirmation_out(c: AgentConfirmation) -> Confirmation:
    return Confirmation(id=c.id, tool=c.tool, summary=c.summary, status=confirmation_status(c),
                        expires_at=iso(c.expires_at))


def create_confirmation(db: Session, user_id: str, thread_id: int, tool: str, args: dict, summary: str, *,
                        run_id: Optional[int] = None, ttl_minutes: int = CONFIRMATION_TTL_MINUTES
                        ) -> AgentConfirmation:
    get_thread(db, thread_id, user_id)
    c = AgentConfirmation(id="c_" + secrets.token_hex(6), user_id=user_id, thread_id=thread_id, run_id=run_id,
                          tool=tool, args=dict(args), summary=summary[:400], status="pending",
                          expires_at=_now() + timedelta(minutes=ttl_minutes), created_at=_now())
    db.add(c)
    db.flush()
    return c


def get_confirmation(db: Session, confirmation_id: str, user_id: str, *, thread_id: Optional[int] = None,
                     require_pending: bool = False) -> AgentConfirmation:
    """NotFound when missing, not the user's, or (with thread_id) on another thread. A pending ticket
    past expires_at is stored as expired here. require_pending raises WrongState unless pending."""
    c = _owned(db.get(AgentConfirmation, confirmation_id), user_id)
    if thread_id is not None and c.thread_id != thread_id:
        raise NotFound()
    if confirmation_status(c) == "expired" and c.status == "pending":
        c.status = "expired"
        db.flush()
    if require_pending and c.status != "pending":
        raise WrongState(c)
    return c


def decide_confirmation(db: Session, confirmation_id: str, user_id: str, status: str, *,
                        result: Optional[dict] = None) -> AgentConfirmation:
    """pending -> approved | declined | failed | expired, once. Anything else raises WrongState."""
    if status not in ("approved", "declined", "failed", "expired"):
        raise ValueError(f"decide_confirmation status {status!r}")
    c = _owned(db.get(AgentConfirmation, confirmation_id), user_id)
    if c.status != "pending":
        raise WrongState(c)
    c.status, c.decided_at = status, _now()
    if result is not None:
        c.result = dict(result)
    db.flush()
    return c


def claim_confirmation(db: Session, confirmation_id: str, user_id: str, *, thread_id: Optional[int] = None) -> bool:
    """pending and unexpired -> approved, in one conditional UPDATE, BEFORE the
    stored action runs. True when this caller claimed it; False when it was
    already decided, expired or not theirs (then nothing may run). Flushes."""
    now = _now()
    q = update(AgentConfirmation).where(
        AgentConfirmation.id == confirmation_id, AgentConfirmation.user_id == user_id,
        AgentConfirmation.status == "pending", AgentConfirmation.expires_at > now)
    if thread_id is not None:
        q = q.where(AgentConfirmation.thread_id == thread_id)
    res = db.execute(q.values(status="approved", decided_at=now).execution_options(synchronize_session=False))
    db.flush()
    return res.rowcount == 1


def record_confirmation_result(db: Session, confirmation_id: str, user_id: str, *, ok: bool) -> AgentConfirmation:
    """After a claimed ticket's action ran: store the result, and move it to
    failed when the action did not succeed. Flushes."""
    c = _owned(db.get(AgentConfirmation, confirmation_id), user_id)
    db.refresh(c)
    c.result = {"ok": bool(ok)}
    if not ok and c.status == "approved":
        c.status = "failed"
    db.flush()
    return c


# ---------------------------------------------------------------- memories

def memory_out(m: AgentMemory) -> Memory:
    return Memory(id=m.id, text=m.text, scope=m.scope, company=m.company, use_in_emails=bool(m.use_in_emails),
                  source=m.source, created_at=iso(m.created_at))


def list_memories(db: Session, user_id: str, *, scopes: Optional[tuple] = None,
                  use_in_emails: Optional[bool] = None) -> list[AgentMemory]:
    q = db.query(AgentMemory).filter(AgentMemory.user_id == user_id)
    if scopes:
        q = q.filter(AgentMemory.scope.in_(list(scopes)))
    if use_in_emails is not None:
        q = q.filter(AgentMemory.use_in_emails.is_(use_in_emails))
    return q.order_by(AgentMemory.created_at.desc(), AgentMemory.id.desc()).all()


def save_memory(db: Session, user_id: str, text: str, *, scope: str = "everywhere", company: Optional[str] = None,
                use_in_emails: bool = False, source: str = "chat") -> AgentMemory:
    """Same text and scope again returns the existing row. The 31st raises WrongState(reason=memory_cap)."""
    if scope not in MEMORY_SCOPES or source not in MEMORY_SOURCES:
        raise ValueError("bad memory scope or source")
    text = " ".join((text or "").split())[:400]
    if not text:
        raise ValueError("empty memory")
    rows = list_memories(db, user_id)
    for m in rows:
        if m.text.lower() == text.lower() and m.scope == scope and (m.company or None) == (company or None):
            return m
    if len(rows) >= MEMORY_CAP:
        raise WrongState(None, "memory_cap")
    m = AgentMemory(user_id=user_id, text=text, scope=scope, company=(company or None) and company[:200],
                    use_in_emails=bool(use_in_emails), source=source, created_at=_now())
    db.add(m)
    db.flush()
    return m


def delete_memory(db: Session, memory_id: int, user_id: str) -> Memory:
    """Hard delete. Returns what was removed (as the schema model, so it outlives the row)."""
    m = _owned(db.get(AgentMemory, memory_id), user_id)
    out = memory_out(m)
    db.delete(m)
    db.flush()
    return out


def touch_memories(db: Session, memory_ids: list[int], user_id: str) -> None:
    if memory_ids:
        db.execute(update(AgentMemory).where(AgentMemory.id.in_(memory_ids), AgentMemory.user_id == user_id)
                   .values(last_used_at=_now()))
        db.flush()


# ---------------------------------------------------------------- settings

def _settings_out(row: Optional[AgentSetting]) -> AgentSettings:
    if row is None:
        return AgentSettings(review_before_send=True, followups=True, never_contact=[])
    items = []
    for it in row.never_contact or []:
        try:
            items.append(NeverContact.model_validate(it))
        except Exception:  # noqa: BLE001 - a stale item is dropped, never fatal
            continue
    return AgentSettings(review_before_send=bool(row.review_before_send), followups=bool(row.followups),
                         never_contact=items)


def get_settings(db: Session, user_id: str) -> AgentSettings:
    """Defaults (review on, follow-ups on, nobody blocked) when the user has no row."""
    return _settings_out(db.get(AgentSetting, user_id))


def update_settings(db: Session, user_id: str, *, review_before_send: Optional[bool] = None,
                    followups: Optional[bool] = None, never_contact: Optional[list] = None) -> AgentSettings:
    """Upsert; a None argument leaves that switch alone. never_contact replaces the whole list."""
    row = db.get(AgentSetting, user_id)
    if row is None:
        row = AgentSetting(user_id=user_id, review_before_send=True, followups=True, never_contact=[])
        db.add(row)
    if review_before_send is not None:
        row.review_before_send = bool(review_before_send)
    if followups is not None:
        row.followups = bool(followups)
    if never_contact is not None:
        seen, items = set(), []
        for it in never_contact:
            nc = it if isinstance(it, NeverContact) else NeverContact.model_validate(it)
            key = (nc.kind, nc.value.strip().lower())
            if key not in seen:
                seen.add(key)
                items.append({"kind": nc.kind, "value": nc.value.strip()})
        row.never_contact = items
    row.updated_at = _now()
    db.flush()
    return _settings_out(row)
