"""Test harness for the student agent. Imported by tests only; never by app code.

    engine = make_engine()                      # SQLite in memory, every table SQLite can build
    use_engine(monkeypatch, engine)             # every SessionLocal the agent opens points here
    db = session_factory(engine)()
    s = seed_student(db)                        # user, candidate, 12 ranked leads, a campaign
    fake = FakeLLM([tool("list_matches", {"limit": 3}), text("Here are three.")]).install(monkeypatch)
    writes = patch_writer(monkeypatch)          # the email generator, faked
    inline_drafting(monkeypatch)                # drafts written on the test thread
    run_id, _, thread_id = start_turn(db, s.user_id, "Who should I write to?")
    runtime.run_turn(run_id, q := queue.Queue(), None); frames = drain(q)

`FakeLLM` replaces `services.agent.llm.stream_chat`, the only seam to the
model. Each call consumes one script item: `text(...)`, `tool(...)`, `fail()`,
a list of them (several tool calls, optionally with text, in one step), or a
callable `(messages, tools) -> item` for a step that depends on what it saw.
Every call is recorded in `.seen` as {"messages", "tools"}.
"""

from __future__ import annotations

import copy
import itertools
import json
import queue
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Callable, Optional

from sqlalchemy import ARRAY, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import (
    Base, Campaign, Candidate, EmailAccount, EmailSent, Lead, LeadScore, User, UserCredit,
)


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


# ---------------------------------------------------------------- database

def _sqlite_ok(table) -> bool:
    return not any(isinstance(c.type, ARRAY) for c in table.columns)


def make_engine():
    """In-memory SQLite shared by every thread (StaticPool), with the agent tables and every core
    table SQLite can build (all but the ARRAY ones), so reads, gating and the runtime all work."""
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[t for t in Base.metadata.sorted_tables if _sqlite_ok(t)])
    return engine


def session_factory(engine):
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def use_engine(monkeypatch, engine):
    """Point database.session.SessionLocal, and any agent module's own SessionLocal, at `engine`."""
    import database.session as session_mod

    factory = session_factory(engine)
    monkeypatch.setattr(session_mod, "SessionLocal", factory)
    monkeypatch.setattr(session_mod, "engine", engine, raising=False)
    for name, mod in list(sys.modules.items()):
        if (name.startswith("services.agent") or name == "api.routes_agent") and \
                getattr(mod, "SessionLocal", None) is not None:
            monkeypatch.setattr(mod, "SessionLocal", factory)
    return factory


def inline_drafting(monkeypatch) -> None:
    """Write drafts on the caller's thread. StaticPool gives every thread the same SQLite
    connection, so the pod's writer threads would share (and roll back) each other's
    transactions; a test that drafts through a turn or route should call this."""
    import services.agent.drafting as drafting

    monkeypatch.setattr(drafting, "RUN_INLINE", True)


# ---------------------------------------------------------------- seed data

_NAMES = ["Ananya Rao", "Karan Mehta", "Priya Nair", "Rohit Shah", "Meera Iyer", "Vikram Das", "Sara Khan",
          "Arjun Pillai", "Neha Gupta", "Dev Malhotra", "Isha Kapoor", "Kabir Sethi", "Tara Bose", "Nikhil Rao"]
_TITLES = ["Engineering Lead", "CTO", "Head of Product", "Founder", "Engineering Manager", "VP Engineering"]


def seed_student(db, matches: int = 12, with_email: int = 2, campaign: bool = True, *, user_id: str = "u1",
                 name: str = "Asha Verma", email: Optional[str] = None, credits: int = 10,
                 now: Optional[datetime] = None) -> SimpleNamespace:
    """One student with a candidate and `matches` scored leads, best first (score 90, 87, ...).
    The first `with_email` leads have a verified address; with `campaign`, a Gmail account, a running
    campaign and one Touch 1 email per addressed lead (the first replied, the second sent with a
    follow-up planned, the rest queued). The last two leads are broader matches (title -25, Recruiter).
    Returns ids plus `addresses`, every address string seeded, for leak checks. Commits."""
    now = now or datetime.utcnow()
    email = email or f"{user_id}.student@example.com"
    db.add(User(id=user_id, email=email, name=name, email_verified=True, created_at=now, updated_at=now))
    db.add(UserCredit(user_id=user_id, total_credits=credits, used_credits=0))
    cand = Candidate(
        user_id=user_id, resume_text="Built Splitkart, a UPI bill splitter.",
        parsed_json={"name": name, "skills": ["Python", "React", "SQL"],
                     "education": [{"institution": "Example University", "degree": "B.Tech"}]},
        resume_profile={"summary": "Final year student who ships small products.",
                        "top_skills": ["Python", "React"], "career_stage": "student"},
        target_roles=["Software Engineer Intern"], target_industries=["Fintech"], dream_companies=["Company1"],
        flex_notes={"best_project": "Built Splitkart, used by 1,200 students"}, created_at=now)
    db.add(cand)
    db.flush()
    leads = []
    for i in range(matches):
        broader = i >= matches - 2 and matches > 2
        lead = Lead(candidate_id=cand.id, name=_NAMES[i % len(_NAMES)] + ("" if i < len(_NAMES) else f" {i}"),
                    title="Recruiter" if broader else _TITLES[i % len(_TITLES)], company=f"Company{i}",
                    company_domain=f"company{i}.example", industry="Fintech", location="Bengaluru",
                    linkedin_url=f"https://www.linkedin.com/in/seed-{user_id}-{i}", company_size="11-50",
                    email=f"hm{i}.{user_id}@company{i}.example" if i < with_email else None,
                    email_verified=i < with_email, status="new", created_at=now)
        db.add(lead)
        db.flush()
        just = ({"headline": f"Builds payments tools at Company{i}", "signal_strength": "high",
                 "bullets": ["Hires interns", "Uses Python", "Ships fintech"]} if i < matches // 2 else None)
        db.add(LeadScore(lead_id=lead.id, overall_score=90 - 3 * i, title_relevance=-25 if broader else 20,
                         department_relevance=10, industry_relevance=10, seniority_relevance=10,
                         location_relevance=10, justification_json=just, created_at=now))
        leads.append(lead)
    seed = SimpleNamespace(user_id=user_id, user_email=email, name=name, candidate_id=cand.id,
                           lead_ids=[lead.id for lead in leads], address_lead_ids=[lead.id for lead in leads[:with_email]],
                           addresses={lead.email for lead in leads if lead.email}, campaign_id=None, account_id=None,
                           email_ids=[], gmail_address=None)
    if campaign:
        acct = EmailAccount(user_id=user_id, email_address=f"{user_id}.mailbox@gmail.example", provider="gmail",
                            access_token="test-access", refresh_token="test-refresh",  # noqa: S106
                            created_at=now)
        db.add(acct)
        db.flush()
        camp = Campaign(candidate_id=cand.id, email_account_id=acct.id, name="Seed campaign", status="running",
                        started_at=now - timedelta(days=3), created_at=now - timedelta(days=3))
        db.add(camp)
        db.flush()
        seed.campaign_id, seed.account_id, seed.gmail_address = camp.id, acct.id, acct.email_address
        for i, lead in enumerate(leads[:with_email]):
            status = "replied" if i == 0 else ("sent" if i == 1 else "queued")
            touch = EmailSent(campaign_id=camp.id, lead_id=lead.id, to_email=lead.email, subject=f"quick question {i}",
                              body=f"Hi {lead.name.split()[0]}, I built Splitkart.", status=status,
                              followup_number=0, is_test=False, assigned_style="warm_intro",
                              sent_at=now - timedelta(days=2) if status != "queued" else None,
                              scheduled_at=now - timedelta(days=2), created_at=now - timedelta(days=2),
                              reply_text="Happy to chat next week." if status == "replied" else None,
                              reply_sentiment="positive" if status == "replied" else None,
                              reply_received_at=now - timedelta(days=1) if status == "replied" else None)
            db.add(touch)
            db.flush()
            seed.email_ids.append(touch.id)
            if status in ("replied", "sent"):
                db.add(EmailSent(campaign_id=camp.id, lead_id=lead.id, to_email=lead.email, followup_number=1,
                                 parent_email_id=touch.id, is_test=False, scheduled_at=now + timedelta(days=2),
                                 status="cancelled_reply" if status == "replied" else "followup_pending",
                                 created_at=now))
    db.commit()
    return seed


# ---------------------------------------------------------------- turns

def start_turn(db, user_id: str, content: str = "Hi", *, thread_id: Optional[int] = None,
               client_turn_id: Optional[str] = None, page_context: Optional[dict] = None,
               confirmation: Optional[dict] = None) -> tuple[int, int, int]:
    """What route 27 commits before streaming: (run_id, user_message_id, thread_id)."""
    from services.agent import store
    from services.agent.schemas import SendMessage

    if thread_id is None:
        thread_id = store.create_thread(db, user_id).id
        db.commit()
    body = SendMessage(content=content, client_turn_id=client_turn_id or uuid.uuid4().hex,
                       page_context=page_context, confirmation=confirmation)
    run_id, msg_id = store.start_run(db, thread_id, user_id, body)
    return run_id, msg_id, thread_id


def drain(q: "queue.Queue") -> list[dict]:
    """Frames a finished run_turn left on its queue, up to the closing None."""
    frames = []
    while True:
        try:
            item = q.get_nowait()
        except queue.Empty:
            return frames
        if item is None:
            return frames
        frames.append(item)


# ---------------------------------------------------------------- the fake model

@dataclass
class Text:
    content: str


@dataclass
class Tool:
    name: str
    args: dict = field(default_factory=dict)
    call_id: Optional[str] = None
    raw: Optional[str] = None          # exact arguments text, for invalid JSON tests


@dataclass
class Fail:
    exc: Optional[BaseException] = None


def text(content: str) -> Text:
    return Text(content)


def tool(name: str, args: Optional[dict] = None, call_id: Optional[str] = None, *, raw: Optional[str] = None) -> Tool:
    return Tool(name, dict(args or {}), call_id, raw)


def fail(exc: Optional[BaseException] = None) -> Fail:
    """Raise from the model call; default llm.ModelUnavailable."""
    return Fail(exc)


@dataclass
class _FallbackCall:
    id: str
    name: str
    arguments: str = "{}"


@dataclass
class _FallbackTurn:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    finish_reason: Optional[str] = None
    stopped: bool = False


class FakeLLM:
    """Scripted stand-in for llm.stream_chat. When the script runs out it answers "Done."."""

    def __init__(self, script: list, *, chunk: int = 12, usage: tuple[int, int] = (120, 30)):
        self.script = list(script)
        self.chunk = max(1, chunk)
        self.usage = usage
        self.seen: list[dict] = []
        self._ids = itertools.count(1)

    def install(self, monkeypatch) -> "FakeLLM":
        from services.agent import llm

        monkeypatch.setattr(llm, "stream_chat", self)
        return self

    @property
    def calls(self) -> int:
        return len(self.seen)

    def __call__(self, messages: list, tools: Optional[list] = None, on_text: Optional[Callable] = None,
                 should_stop: Optional[Callable] = None, **_: Any):
        self.seen.append({"messages": copy.deepcopy(messages), "tools": tools})
        item: Any = self.script.pop(0) if self.script else Text("Done.")
        if callable(item) and not isinstance(item, (Text, Tool, Fail)):
            item = item(messages, tools)
        items = list(item) if isinstance(item, (list, tuple)) else [item]
        for it in items:
            if isinstance(it, Fail):
                if it.exc is not None:
                    raise it.exc
                from services.agent import llm
                raise llm.ModelUnavailable("scripted failure")
        words, stopped = [], False
        for it in (i for i in items if isinstance(i, Text)):
            for start in range(0, len(it.content), self.chunk):
                if should_stop is not None and should_stop():
                    stopped = True
                    break
                piece = it.content[start:start + self.chunk]
                words.append(piece)
                if on_text is not None:
                    on_text(piece)
            if stopped:
                break
        calls = [] if stopped else [i for i in items if isinstance(i, Tool)]
        return self._turn("".join(words), calls, stopped)

    def _turn(self, said: str, calls: list, stopped: bool):
        try:
            from services.agent import llm
            turn_cls, call_cls = llm.Turn, llm.ToolCall
        except Exception:  # noqa: BLE001 - before llm.py exists
            turn_cls, call_cls = _FallbackTurn, _FallbackCall
        tool_calls = [call_cls(id=c.call_id or f"call_{next(self._ids)}", name=c.name,
                               arguments=c.raw if c.raw is not None else json.dumps(c.args)) for c in calls]
        usage = {"prompt_tokens": self.usage[0], "completion_tokens": self.usage[1]}
        return turn_cls(text=said, tool_calls=tool_calls, usage=usage,
                        finish_reason="tool_calls" if tool_calls else "stop", stopped=stopped)


# ---------------------------------------------------------------- the email writer

def patch_writer(monkeypatch, *, subject: str = "quick question {first}",
                 body: str = "Hi {first},\n\nI built Splitkart, a UPI bill splitter. Could we talk?\n\nAsha",
                 revise: Optional[dict] = None, error: Optional[BaseException] = None) -> list:
    """Fake generate_email_for_lead and generate_json wherever drafting can reach them. Returns the
    call log: ("generate", lead_id, style) and ("revise", messages-or-prompt) tuples."""
    log: list = []

    def fake_generate(lead, candidate, style, user_name=""):
        log.append(("generate", getattr(lead, "id", None), style))
        if error is not None:
            raise error
        first = ((getattr(lead, "name", "") or "there").split() or ["there"])[0]
        return subject.format(first=first), body.format(first=first)

    def fake_generate_json(*args, **kwargs):
        log.append(("revise", args, kwargs))
        if error is not None:
            raise error
        return dict(revise or {"subject": "shorter question", "body": "Hi there,\n\nShort version.\n\nAsha"})

    targets = ["services.email_campaign.email_generator_service"]
    try:
        import services.agent.drafting  # noqa: F401
        targets.insert(0, "services.agent.drafting")
    except Exception:  # noqa: BLE001 - drafting.py not written yet
        pass
    for name in targets:
        mod = sys.modules.get(name)
        if mod is None:
            continue
        monkeypatch.setattr(mod, "generate_email_for_lead", fake_generate, raising=False)
        if name == "services.agent.drafting" or hasattr(mod, "generate_json"):
            monkeypatch.setattr(mod, "generate_json", fake_generate_json, raising=False)
    return log


# ---------------------------------------------------------------- the flag and the HTTP surface

def enable_agent(monkeypatch, allowlist: str = "*") -> None:
    """AGENT_ENABLED on, the given allowlist, kill switch off."""
    from core.config import settings

    monkeypatch.setattr(settings, "AGENT_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "AGENT_ALLOWLIST", allowlist, raising=False)
    monkeypatch.delenv("AGENT_DISABLED", raising=False)


def agent_client(engine, user_id: Optional[str]):
    """A TestClient over the agent router alone (mounted at /api/v1), signed in as `user_id`
    (None means signed out: get_current_user is left as it is)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api import routes_agent
    from api.dependencies import get_current_user
    from database.session import get_db

    factory = session_factory(engine)
    app = FastAPI()
    app.include_router(routes_agent.router, prefix="/api/v1")

    def _db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    def _user():
        db = factory()
        try:
            user = db.get(User, user_id)
            db.expunge(user)
            return user
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    if user_id is not None:
        app.dependency_overrides[get_current_user] = _user
    return TestClient(app)
