"""All reads and writes of the eight agent tables (migration 076). Owner: BE-C.

Rules every caller can rely on:
- Every public function takes `db` first. Functions on one object take its id
  next, then `user_id` (`get_thread(db, thread_id, user_id)`); functions on the
  user's collection take `user_id` next (`list_threads(db, user_id)`). Missing
  and not yours both raise `NotFound`, so a probe learns nothing.
- Run-path functions called by the worker thread with a run id it created
  (`append_event, add_usage, heartbeat, cancel_requested, save_*, finish_run,
  fail_run`) take `user_id` as an optional keyword; when given it is enforced.
- Commits: `start_run`, `request_cancel`, the reapers and every run-path
  function commit (pass `commit=False` to batch). Everything else only
  flushes, and the route or action that called it commits.
- A stale `expected_version` raises `VersionConflict(current_row)`. A row in a
  state the call cannot work on raises `WrongState(row)`.
- JSON columns are always reassigned, never mutated in place.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.models import (
    AgentConfirmation, AgentDraft, AgentMessage, AgentRun, AgentThread,
    AgentUsageDaily,
)
from services.agent.schemas_chat import (
    Card, Message, NavigateTarget, Step, Suggestion, Thread,
)

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_TITLE = "New chat"
THREAD_STATUSES = ("active", "archived")
RUN_STATUSES = ("running", "done", "error", "cancelled", "limit")
TERMINAL_RUN_STATUSES = ("done", "error", "cancelled", "limit")
MESSAGE_ROLES = ("user", "assistant", "tool")
DRAFT_STATUSES = ("drafting", "needs_review", "approved", "skipped", "failed", "discarded", "superseded", "sent")
LIVE_DRAFT_STATUSES = ("drafting", "needs_review", "approved", "skipped")
WAITING_DRAFT_STATUSES = ("drafting", "needs_review")
CONFIRMATION_STATUSES = ("pending", "approved", "declined", "expired", "failed")
MEMORY_SCOPES = ("everywhere", "emails", "matching", "company")
MEMORY_SOURCES = ("chat", "settings", "profile")
MEMORY_CAP = 30
TOOL_CONTENT_CAP = 6000
STALE_RUN_SECONDS = 90
STALE_DRAFT_SECONDS = 300
CONFIRMATION_TTL_MINUTES = 30
INTERRUPTED_MESSAGE = "That turn was interrupted. Please send it again."
DRAFT_FAILED_REASON = "timed_out"


class NotFound(Exception):
    """Missing, or not this user's."""


class VersionConflict(Exception):
    def __init__(self, current: AgentDraft):
        super().__init__("version_conflict")
        self.current = current


class DuplicateTurn(Exception):
    def __init__(self, run_id: Optional[int]):
        super().__init__("duplicate_turn")
        self.run_id = run_id


class WrongState(Exception):
    def __init__(self, row: Any = None, reason: str = "wrong_state"):
        super().__init__(reason)
        self.row = row
        self.reason = reason


def _now() -> datetime:
    return datetime.utcnow()


def iso(dt: Optional[datetime]) -> Optional[str]:
    """Naive UTC ISO string, seconds precision (contract section 2)."""
    return dt.replace(microsecond=0).isoformat() if dt else None


def _owned(row, user_id: Optional[str]):
    if row is None or (user_id is not None and row.user_id != user_id):
        raise NotFound()
    return row


# ---------------------------------------------------------------- IST day and usage

class DayUsage(dict):
    """{messages, drafts, revisions, tokens} for one IST day; keys also read as attributes."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as e:
            raise AttributeError(name) from e


def ist_day(now: Optional[datetime] = None) -> date:
    now = now or _now()
    return now.replace(tzinfo=ZoneInfo("UTC")).astimezone(IST).date()


def resets_at(now: Optional[datetime] = None) -> str:
    """Next IST midnight as a naive UTC ISO string (the daily_limit `resets_at`)."""
    day = ist_day(now) + timedelta(days=1)
    midnight = datetime(day.year, day.month, day.day, tzinfo=IST).astimezone(ZoneInfo("UTC"))
    return iso(midnight.replace(tzinfo=None))


def usage_today(db: Session, user_id: str, *, now: Optional[datetime] = None) -> DayUsage:
    row = db.get(AgentUsageDaily, (user_id, ist_day(now)))
    return DayUsage(messages=row.messages if row else 0, drafts=row.drafts if row else 0,
                    revisions=row.revisions if row else 0, tokens=row.tokens if row else 0)


def bump_usage(db: Session, user_id: str, *, messages: int = 0, drafts: int = 0, revisions: int = 0,
               tokens: int = 0, now: Optional[datetime] = None) -> DayUsage:
    """Add to today's counters (upsert, safe under concurrent callers). Flushes."""
    day = ist_day(now)
    vals = {"messages": messages, "drafts": drafts, "revisions": revisions, "tokens": tokens}
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    stmt = insert(AgentUsageDaily).values(user_id=user_id, day=day, **vals)
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id", "day"],
        set_={k: getattr(AgentUsageDaily, k) + v for k, v in vals.items()},
    )
    db.execute(stmt)
    db.flush()
    row =db.get(AgentUsageDaily, (user_id, day), populate_existing=True)
    return DayUsage(messages=row.messages, drafts=row.drafts, revisions=row.revisions, tokens=row.tokens)


# ---------------------------------------------------------------- threads

def thread_out(t: AgentThread) -> Thread:
    return Thread(id=t.id, title=t.title, scope=t.scope, updated_at=iso(t.updated_at))


def get_thread(db: Session, thread_id: int, user_id: str) -> AgentThread:
    return _owned(db.get(AgentThread, thread_id), user_id)


def list_threads(db: Session, user_id: str, scope: Optional[str] = None, limit: int = 20) -> list[AgentThread]:
    q = db.query(AgentThread).filter(AgentThread.user_id == user_id, AgentThread.status == "active")
    if scope:
        q = q.filter(AgentThread.scope == scope)
    return q.order_by(AgentThread.updated_at.desc(), AgentThread.id.desc()).limit(limit).all()


def create_thread(db: Session, user_id: str, *, scope: str = "main", title: Optional[str] = None,
                  candidate_id: Optional[int] = None) -> AgentThread:
    now = _now()
    t = AgentThread(user_id=user_id, scope=scope, title=(title or DEFAULT_TITLE)[:80], status="active",
                    candidate_id=candidate_id, created_at=now, updated_at=now)
    db.add(t)
    db.flush()
    return t


def get_or_create_scoped_thread(db: Session, user_id: str, scope: str, *, title: Optional[str] = None,
                                candidate_id: Optional[int] = None) -> AgentThread:
    """The one active thread for an `outreach:<id>` scope; `main` always creates a new one."""
    if scope == "main":
        return create_thread(db, user_id, scope=scope, title=title, candidate_id=candidate_id)
    found = list_threads(db, user_id, scope=scope, limit=1)
    if found:
        return found[0]
    try:
        with db.begin_nested():
            return create_thread(db, user_id, scope=scope, title=title, candidate_id=candidate_id)
    except IntegrityError:
        found = list_threads(db, user_id, scope=scope, limit=1)
        if not found:
            raise
        return found[0]


def archive_thread(db: Session, thread_id: int, user_id: str) -> AgentThread:
    t = get_thread(db, thread_id, user_id)
    t.status = "archived"
    t.updated_at = _now()
    db.flush()
    return t


def touch_thread(db: Session, thread_id: int, user_id: Optional[str] = None, *, title: Optional[str] = None,
                 candidate_id: Optional[int] = None) -> AgentThread:
    """Bump updated_at; set the title only while it is still the default; pin the candidate."""
    t = _owned(db.get(AgentThread, thread_id), user_id)
    t.updated_at = _now()
    if title and t.title == DEFAULT_TITLE:
        t.title = " ".join(title.split())[:80] or DEFAULT_TITLE
    if candidate_id is not None:
        t.candidate_id = candidate_id
    db.flush()
    return t


# ---------------------------------------------------------------- messages

_CARD = TypeAdapter(Card)


def _valid(adapter_or_model, items) -> list:
    """Validate stored dicts one by one; a stale shape is dropped, never fatal for the page."""
    out = []
    for it in items or []:
        try:
            out.append(adapter_or_model.validate_python(it) if isinstance(adapter_or_model, TypeAdapter)
                       else adapter_or_model.model_validate(it))
        except ValidationError:
            continue
    return out


def message_out(db: Session, m: AgentMessage, run: Optional[AgentRun] = None) -> Message:
    meta = m.meta or {}
    if m.role == "user":
        return Message(id=m.id, role="user", content=m.content or "", created_at=iso(m.created_at), run_id=m.run_id)
    conf_ids = [c for c in meta.get("confirmation_ids") or [] if isinstance(c, str)]
    confs = []
    if conf_ids:
        rows = db.query(AgentConfirmation).filter(AgentConfirmation.id.in_(conf_ids),
                                                  AgentConfirmation.user_id == m.user_id).all()
        by_id = {c.id: c for c in rows}
        confs = [confirmation_out(by_id[c]) for c in conf_ids if c in by_id]
    nav = _valid(NavigateTarget, [meta["navigate"]]) if meta.get("navigate") else []
    status = meta.get("status") or (run.status if run is not None else None)
    return Message(
        id=m.id, role="assistant", content=m.content or "", created_at=iso(m.created_at), run_id=m.run_id,
        status=status if status in RUN_STATUSES else None,
        steps=_valid(Step, meta.get("steps")), cards=_valid(_CARD, meta.get("cards")),
        outreach_ids=[str(x) for x in meta.get("outreach_ids") or []], confirmations=confs,
        navigate=nav[0] if nav else None, suggestions=_valid(Suggestion, meta.get("suggestions"))[:3],
    )


def _display_filter(q):
    # User rows, and the one final assistant row per run (tool-call rows carry tool_calls).
    return q.filter((AgentMessage.role == "user")
                    | ((AgentMessage.role == "assistant") & AgentMessage.tool_calls.is_(None)))


def display_messages(db: Session, thread_id: int, user_id: str, before: Optional[int] = None,
                     limit: int = 50) -> tuple[list[Message], bool]:
    """Chat bubbles oldest first: the last `limit` before message id `before`, and whether more exist."""
    get_thread(db, thread_id, user_id)
    q = _display_filter(db.query(AgentMessage).filter(AgentMessage.thread_id == thread_id,
                                                      AgentMessage.user_id == user_id))
    if before:
        q = q.filter(AgentMessage.id < before)
    rows = q.order_by(AgentMessage.id.desc()).limit(max(1, limit) + 1).all()
    has_more = len(rows) > limit
    rows = list(reversed(rows[:limit]))
    return [message_out(db, m) for m in rows], has_more


def final_message(db: Session, run_id: int, user_id: str) -> Optional[Message]:
    """The run's final assistant message, once it has one (RunPoll.message)."""
    run = get_run(db, run_id, user_id)
    m = (_display_filter(db.query(AgentMessage).filter(AgentMessage.run_id == run_id,
                                                       AgentMessage.role == "assistant"))
         .order_by(AgentMessage.id.desc()).first())
    return message_out(db, m, run) if m else None


def _as_model_message(m: AgentMessage, summarise: bool) -> dict:
    if m.role == "tool":
        content = ((m.meta or {}).get("summary") or "done") if summarise else (m.content or "")
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": content}
    if m.role == "assistant" and m.tool_calls:
        return {"role": "assistant", "content": m.content or None, "tool_calls": m.tool_calls}
    return {"role": m.role, "content": m.content or ""}


def _complete_tool_pairs(rows: list[AgentMessage]) -> list[AgentMessage]:
    """Drop tool-call rows whose calls lack a result (a run that died mid step), and orphan results."""
    answered = {m.tool_call_id for m in rows if m.role == "tool"}
    keep, called = [], set()
    for m in rows:
        if m.role == "assistant" and m.tool_calls:
            ids = {c.get("id") for c in m.tool_calls if isinstance(c, dict)}
            if not ids or not ids <= answered:
                continue
            called |= ids
        if m.role == "tool" and m.tool_call_id not in called:
            continue
        keep.append(m)
    return keep


def history_for_model(db: Session, thread_id: int, user_id: str, *, max_runs: int = 12, max_chars: int = 24000,
                      full_tool_runs: int = 3, exclude_run_id: Optional[int] = None) -> list[dict]:
    """OpenAI chat messages for the thread (design 4.5): the last `max_runs` runs whole, oldest
    whole runs cut while over `max_chars`, tool results older than the last `full_tool_runs`
    runs replaced by their summary. Includes the current run's user message (it was committed
    by start_run) unless `exclude_run_id` names that run."""
    get_thread(db, thread_id, user_id)
    rows = (db.query(AgentMessage).filter(AgentMessage.thread_id == thread_id, AgentMessage.user_id == user_id)
            .order_by(AgentMessage.id.asc()).all())
    groups: list[list[AgentMessage]] = []
    for m in rows:
        if exclude_run_id is not None and m.run_id == exclude_run_id:
            continue
        if groups and m.run_id is not None and groups[-1][0].run_id == m.run_id:
            groups[-1].append(m)
        else:
            groups.append([m])
    groups = [_complete_tool_pairs(g) for g in groups[-max_runs:]]
    out = [[_as_model_message(m, summarise=i < len(groups) - full_tool_runs) for m in g]
           for i, g in enumerate(groups)]

    def size(g):
        return sum(len(str(x.get("content") or "")) + len(str(x.get("tool_calls") or "")) for x in g)

    while len(out) > 1 and sum(size(g) for g in out) > max_chars:
        out.pop(0)
    return [m for g in out for m in g]


def add_user_message(db: Session, thread_id: int, user_id: str, content: str, *, run_id: Optional[int] = None,
                     client_turn_id: Optional[str] = None, meta: Optional[dict] = None) -> AgentMessage:
    get_thread(db, thread_id, user_id)
    m = AgentMessage(thread_id=thread_id, user_id=user_id, run_id=run_id, role="user", content=content or "",
                     client_turn_id=client_turn_id, meta=dict(meta or {}), created_at=_now())
    db.add(m)
    db.flush()
    return m


def _run_row(db: Session, run_id: int, user_id: Optional[str] = None) -> AgentRun:
    return _owned(db.get(AgentRun, run_id), user_id)


def save_assistant_tool_calls(db: Session, run_id: int, tool_calls: list[dict], content: str = "", *,
                              user_id: Optional[str] = None, commit: bool = True) -> AgentMessage:
    """The model's tool-calling step, OpenAI shape: [{id, type: function, function: {name, arguments}}]."""
    run = _run_row(db, run_id, user_id)
    m = AgentMessage(thread_id=run.thread_id, user_id=run.user_id, run_id=run.id, role="assistant",
                     content=content or "", tool_calls=list(tool_calls), meta={}, created_at=_now())
    db.add(m)
    db.commit() if commit else db.flush()
    return m


def save_tool_result(db: Session, run_id: int, tool_call_id: str, tool_name: str, content: str, *,
                     ok: bool = True, summary: str = "", user_id: Optional[str] = None,
                     commit: bool = True) -> AgentMessage:
    """What the model saw for one call, capped at 6000 chars, plus {ok, summary} for older turns."""
    run = _run_row(db, run_id, user_id)
    m = AgentMessage(thread_id=run.thread_id, user_id=run.user_id, run_id=run.id, role="tool",
                     content=(content or "")[:TOOL_CONTENT_CAP], tool_call_id=tool_call_id, tool_name=tool_name,
                     meta={"ok": bool(ok), "summary": (summary or "")[:200]}, created_at=_now())
    db.add(m)
    db.commit() if commit else db.flush()
    return m


def save_final_message(db: Session, run_id: int, content: str, *, meta: Optional[dict] = None,
                       user_id: Optional[str] = None) -> AgentMessage:
    """The turn's one chat bubble. meta: {steps, cards, outreach_ids, confirmation_ids, navigate,
    suggestions, status}. Flushes only: call finish_run next so both land in one commit."""
    run = _run_row(db, run_id, user_id)
    m = AgentMessage(thread_id=run.thread_id, user_id=run.user_id, run_id=run.id, role="assistant",
                     content=content or "", tool_calls=None, meta=dict(meta or {}), created_at=_now())
    db.add(m)
    db.flush()
    touch_thread(db, run.thread_id)
    return m


# ---------------------------------------------------------------- the rest of the store
# Runs, tickets, memories, settings and drafts live in two private modules to keep
# files short; they are part of this API. Import them from here only.
from services.agent._store_runs import (  # noqa: E402, F401
    add_usage, append_event, cancel_requested, claim_confirmation, confirmation_out, confirmation_status,
    create_confirmation, decide_confirmation, record_confirmation_result, delete_memory, events_after, fail_run, finish_run, get_confirmation, get_run,
    get_settings, heartbeat, list_memories, live_runs, memory_out, reap_stale_runs, request_cancel, run_out,
    save_memory, start_run, touch_memories, update_settings,
)
from services.agent._store_drafts import (  # noqa: E402, F401
    approval_hash, delete_draft, get_draft, get_draft_internal, heartbeat_draft, insert_drafting, insert_skipped,
    list_drafts, live_rows_by_lead, reap_stale_drafts, set_draft_status, update_draft_copy, waiting_count,
)
