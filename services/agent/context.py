"""Who the agent is working for, and what it may touch.

- `resolve_agent_candidate`: the student's candidate (design 4.3).
- Ownership helpers: every lead, draft and email is reached only through
  `Candidate.user_id == user_id` (leads have no user_id of their own).
- `ToolContext` and `load(run_id)`: what a tool handler gets. The user,
  candidate, thread and run always come from the session, never from model
  arguments.
- `looking_at` and `snapshot`: the per-turn context block (facts about the
  student, page context, memories) rendered by `prompt.build_snapshot`.
  Page context ids are ownership checked and silently dropped when not theirs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from core.logger import get_logger
from database.models import (
    AgentDraft,
    AgentMessage,
    AgentRun,
    AgentThread,
    Campaign,
    Candidate,
    EmailSent,
    Lead,
    User,
)

logger = get_logger(__name__)

# Tests may point this at their own sessionmaker; None means database.session's.
SessionLocal = None

_OUTREACH_ID = re.compile(r"^([de])_([1-9][0-9]*)$")


class ActionError(Exception):
    """A student-facing refusal from reads.* or actions.*. Routes turn it into
    ErrorResponse {"detail": {"code", "message", **extra}} with `status`;
    tools turn it into NOT_FOUND: / REFUSED: text for the model."""

    def __init__(self, code: str, message: str, status: int = 409, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


NOT_FOUND_MESSAGE = "I couldn't find that. It may have been removed."


def not_found(message: str = NOT_FOUND_MESSAGE) -> ActionError:
    return ActionError("not_found", message, 404)


def open_session() -> Session:
    """A fresh short-lived session. Looked up at call time so a test can patch
    either this module's SessionLocal or database.session.SessionLocal."""
    if SessionLocal is not None:
        return SessionLocal()
    from database import session as _session
    return _session.SessionLocal()


def user_id_of(user: Any) -> str:
    """Accept a User row, anything with `.id`, or the id itself."""
    if isinstance(user, str):
        return user
    uid = getattr(user, "id", None)
    return str(uid if uid is not None else user)


def first_name_of(name: Optional[str]) -> str:
    parts = (name or "").strip().split()
    return parts[0].title() if parts and parts[0].isupper() else (parts[0] if parts else "")


# ---------------------------------------------------------------- candidate

def resolve_agent_candidate(db: Session, user_id: Any, thread: Any = None) -> Optional[Candidate]:
    """The candidate the agent works on (design 4.3), or None (no resume yet).

    1. `resolve_next_step(heal=False).candidate_id`, a pure read.
    2. Else the thread's stored candidate if it is still the user's.
    3. Else the newest candidate that has leads, else the newest candidate.
    """
    uid = user_id_of(user_id)
    cid = None
    try:
        from services.next_step import resolve_next_step
        # A savepoint, so a failure here never discards the caller's pending work.
        with db.begin_nested():
            cid = resolve_next_step(db, uid, heal=False).candidate_id
    except Exception as e:
        logger.warning("[AGENT] next-step resolution failed; falling back: %s", type(e).__name__)
        cid = None
    if cid is None and thread is not None:
        cid = getattr(thread, "candidate_id", None)
    if cid is not None:
        cand = db.query(Candidate).filter(Candidate.id == cid, Candidate.user_id == uid).first()
        if cand is not None:
            return cand
    has_leads = db.query(Lead.id).filter(Lead.candidate_id == Candidate.id).exists()
    cand = (
        db.query(Candidate)
        .filter(Candidate.user_id == uid, has_leads)
        .order_by(Candidate.created_at.desc(), Candidate.id.desc())
        .first()
    )
    if cand is not None:
        return cand
    return (
        db.query(Candidate)
        .filter(Candidate.user_id == uid)
        .order_by(Candidate.created_at.desc(), Candidate.id.desc())
        .first()
    )


# ---------------------------------------------------------------- ownership

def parse_outreach_id(outreach_id: Any) -> Optional[tuple[str, int]]:
    """'d_88' -> ('d', 88). None for anything malformed (the caller 404s)."""
    m = _OUTREACH_ID.match(str(outreach_id or ""))
    return (m.group(1), int(m.group(2))) if m else None


def owned_lead(db: Session, user_id: Any, lead_id: Any) -> Optional[Lead]:
    try:
        lid = int(lead_id)
    except (TypeError, ValueError):
        return None
    return (
        db.query(Lead)
        .join(Candidate, Candidate.id == Lead.candidate_id)
        .filter(Lead.id == lid, Candidate.user_id == user_id_of(user_id))
        .first()
    )


def owned_draft(db: Session, user_id: Any, draft_id: int) -> Optional[AgentDraft]:
    return (
        db.query(AgentDraft)
        .join(Candidate, Candidate.id == AgentDraft.candidate_id)
        .filter(AgentDraft.id == draft_id, AgentDraft.user_id == user_id_of(user_id),
                Candidate.user_id == user_id_of(user_id))
        .first()
    )


def owned_email(db: Session, user_id: Any, email_id: int) -> Optional[EmailSent]:
    """A Touch 1, non-test email in a campaign whose candidate is the user's."""
    return (
        db.query(EmailSent)
        .join(Campaign, Campaign.id == EmailSent.campaign_id)
        .join(Candidate, Candidate.id == Campaign.candidate_id)
        .filter(EmailSent.id == email_id, Candidate.user_id == user_id_of(user_id),
                EmailSent.followup_number == 0, EmailSent.is_test.isnot(True))
        .first()
    )


def owned_outreach(db: Session, user_id: Any, outreach_id: Any):
    """('d', AgentDraft) or ('e', EmailSent) when it is the user's, else None."""
    parsed = parse_outreach_id(outreach_id)
    if parsed is None:
        return None
    kind, num = parsed
    row = owned_draft(db, user_id, num) if kind == "d" else owned_email(db, user_id, num)
    return (kind, row) if row is not None else None


# ---------------------------------------------------------------- tool context

@dataclass
class ToolContext:
    user_id: str
    first_name: str = ""
    user_name: str = ""
    candidate_id: Optional[int] = None
    thread_id: Optional[int] = None
    run_id: Optional[int] = None
    page_context: Optional[dict] = None
    scope: str = "main"
    # Per-run budgets, counted by tools.dispatch across the turn.
    tool_calls: int = 0
    drafts_this_run: int = 0
    extra: dict = field(default_factory=dict)


def _page_context_for_run(db: Session, run: AgentRun) -> Optional[dict]:
    msg = (
        db.query(AgentMessage)
        .filter(AgentMessage.run_id == run.id, AgentMessage.role == "user",
                AgentMessage.thread_id == run.thread_id)
        .order_by(AgentMessage.id.desc())
        .first()
    )
    if msg is None:
        msg = (
            db.query(AgentMessage)
            .filter(AgentMessage.thread_id == run.thread_id, AgentMessage.role == "user")
            .order_by(AgentMessage.id.desc())
            .first()
        )
    meta = (msg.meta if msg is not None else None) or {}
    pc = meta.get("page_context")
    return dict(pc) if isinstance(pc, dict) else None


def load(run_id: int, db: Optional[Session] = None) -> ToolContext:
    """Build the ToolContext for a run in its own short session (or `db`).
    Also stores the resolved candidate on the thread."""
    own = db is None
    db = db or open_session()
    try:
        run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
        if run is None:
            raise LookupError(f"agent run {run_id} not found")
        thread = (
            db.query(AgentThread)
            .filter(AgentThread.id == run.thread_id, AgentThread.user_id == run.user_id)
            .first()
        )
        user = db.query(User).filter(User.id == run.user_id).first()
        cand = resolve_agent_candidate(db, run.user_id, thread)
        if thread is not None and cand is not None and thread.candidate_id != cand.id:
            thread.candidate_id = cand.id
        page_context = _page_context_for_run(db, run)
        scope = getattr(thread, "scope", None) or "main"
        if scope.startswith("outreach:"):
            # An outreach thread always carries its own outreach in the context.
            page_context = dict(page_context or {"route": "outreach"})
            page_context["outreach_id"] = page_context.get("outreach_id") or scope.split(":", 1)[1]
        user_name = (getattr(user, "name", None) or "").strip()
        ctx = ToolContext(
            user_id=run.user_id,
            first_name=first_name_of(user_name),
            user_name=user_name,
            candidate_id=cand.id if cand is not None else None,
            thread_id=run.thread_id,
            run_id=run.id,
            page_context=page_context,
            scope=scope,
        )
        if own:
            db.commit()
        return ctx
    except Exception:
        if own:
            db.rollback()
        raise
    finally:
        if own:
            db.close()


# ---------------------------------------------------------------- per-turn context block

def data_field(value: Any, limit: int = 80) -> str:
    """Third-party text (scraped names, titles, companies, search text) made
    safe for the snapshot: one line, capped, inner double quotes softened. The
    caller wraps it in quotes, so a crafted title cannot pose as a rule line."""
    flat = " ".join(str(value or "").split()).replace('"', "'")
    return flat if len(flat) <= limit else flat[:limit - 3].rstrip() + "..."


def _lead_line(lead: Lead) -> str:
    who = f'"{data_field(lead.name)}"' if lead.name else "Someone"
    parts = [f'"{data_field(lead.title)}"' if lead.title else None,
             f'at "{data_field(lead.company)}"' if lead.company else None]
    role = " ".join(x for x in parts if x)
    return f"{who}{', ' + role if role else ''} (lead {lead.id})"


def _outreach_line(db: Session, user_id: str, outreach_id: str) -> Optional[str]:
    from services.agent import status_words

    found = owned_outreach(db, user_id, outreach_id)
    if found is None:
        return None
    kind, row = found
    lead = db.query(Lead).filter(Lead.id == row.lead_id).first()
    if kind == "d":
        r = status_words.draft_row(row, lead)
        label = "draft"
    else:
        r = status_words.email_row(row, lead, [])
        label = "email"
    from services.agent.schemas import STATUS_LABELS_PHASE1

    word = STATUS_LABELS_PHASE1.get(r.status_word, "") if r is not None else "hidden"
    head = _lead_line(lead) if lead is not None else "a person"
    return f"{head}, {label} {outreach_id}, {word}."


def looking_at(db: Session, ctx: ToolContext) -> list[str]:
    """The page context as data lines, ownership checked. Unowned ids vanish."""
    from services.agent.schemas import STATUS_LABELS_PHASE1, STATUS_WORDS

    pc = ctx.page_context or {}
    lines: list[str] = []
    route = pc.get("route")
    if route:
        lines.append(f"Screen: {route}.")
    seen_leads: set[int] = set()
    seen_outreach: set[str] = set()
    oid = pc.get("outreach_id")
    if oid:
        line = _outreach_line(db, ctx.user_id, oid)
        if line:
            lines.append(line)
            seen_outreach.add(str(oid))
    lid = pc.get("lead_id")
    if lid is not None:
        lead = owned_lead(db, ctx.user_id, lid)
        if lead is not None:
            lines.append(_lead_line(lead) + ".")
            seen_leads.add(lead.id)
    filt = pc.get("filter")
    if filt and route == "home" and filt in STATUS_WORDS:
        lines.append(f"Home filter: {STATUS_LABELS_PHASE1[filt]}.")
    query = data_field(pc.get("query"), 120)
    if query:
        lines.append(f'People search: "{query}".')
    selected = []
    for item in (pc.get("selected") or [])[:5]:
        kind, sid = (item or {}).get("kind"), (item or {}).get("id")
        if kind == "lead":
            lead = owned_lead(db, ctx.user_id, sid)
            if lead is not None and lead.id not in seen_leads:
                selected.append(_lead_line(lead))
                seen_leads.add(lead.id)
        elif kind == "outreach" and str(sid) not in seen_outreach:
            line = _outreach_line(db, ctx.user_id, str(sid))
            if line:
                selected.append(line.rstrip("."))
                seen_outreach.add(str(sid))
    if selected:
        lines.append("Selected: " + "; ".join(selected) + ".")
    return lines


def scope_line(db: Session, ctx: ToolContext) -> Optional[str]:
    if not (ctx.scope or "").startswith("outreach:"):
        return None
    oid = ctx.scope.split(":", 1)[1]
    line = _outreach_line(db, ctx.user_id, oid)
    return f"This chat is about one outreach: {line}" if line else None


def snapshot_inputs(ctx: ToolContext, db: Optional[Session] = None) -> dict:
    """Keyword arguments for prompt.build_snapshot: today, first_name, status,
    memories, looking_at, scope_line. Real counts, ownership-checked context,
    no address or phone (Status.gmail.address is removed here)."""
    from services.agent import reads

    own = db is None
    db = db or open_session()
    try:
        status = reads.status(db, ctx.user_id)
        status = status.model_copy(update={"gmail": status.gmail.model_copy(update={"address": None})})
        mems = reads.memories(db, ctx.user_id).items
        out = {
            "today": datetime.utcnow().date().isoformat(),
            "first_name": ctx.first_name or status.first_name,
            "status": status,
            "memories": mems,
            "looking_at": looking_at(db, ctx),
            "scope_line": scope_line(db, ctx),
        }
        if own:
            db.commit()
        return out
    except Exception:
        if own:
            db.rollback()
        raise
    finally:
        if own:
            db.close()


def snapshot(ctx: ToolContext, db: Optional[Session] = None) -> str:
    """The rendered per-turn context block (a second system message)."""
    from services.agent import prompt

    return prompt.build_snapshot(**snapshot_inputs(ctx, db))
