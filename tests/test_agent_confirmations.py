"""Confirmation tickets (contract section 7).

Pins: Confirm runs the ticket's STORED args through the profile-notes action
inside a tool_start/tool_end pair that invalidates profile, marks the ticket
approved and emits it, then asks the model for a text-only acknowledgement;
Cancel changes nothing and marks it declined; an expired, decided or
other-thread ticket is a 409 wrong_state before anything streams.
"""

import json
import queue
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api import routes_agent
from core.config import settings
from database.models import AgentConfirmation, Candidate
from services.agent import gating, runtime, store, testing
from services.agent.schemas import SendMessage


@pytest.fixture
def env(monkeypatch):
    engine = testing.make_engine()
    factory = testing.use_engine(monkeypatch, engine)
    db = factory()
    seed = testing.seed_student(db)
    monkeypatch.setattr(settings, "AGENT_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_ALLOWLIST", "*")
    monkeypatch.delenv("AGENT_DISABLED", raising=False)
    gating.reset_slots_for_tests()
    started = []
    monkeypatch.setattr(routes_agent, "_start_worker", lambda run_id, q, slot, umid, conf: (
        started.append(run_id), runtime.run_turn(run_id, q, slot, user_message_id=umid, confirmation=conf)))
    thread = store.create_thread(db, seed.user_id)
    ticket = store.create_confirmation(db, seed.user_id, thread.id, "update_profile_notes",
                                       {"why_now": "Graduating in May and want a fintech role"},
                                       'Save "Graduating in May" as why now.')
    db.commit()
    user = SimpleNamespace(id=seed.user_id, email=seed.user_email, name=seed.name)
    yield SimpleNamespace(db=db, seed=seed, user=user, thread_id=thread.id, ticket_id=ticket.id,
                          monkeypatch=monkeypatch, started=started)
    db.close()
    gating.reset_slots_for_tests()


def _decide(env, decision, ticket_id=None, thread_id=None, turn="turn-c001"):
    body = SendMessage(content="", client_turn_id=turn,
                       confirmation={"id": ticket_id or env.ticket_id, "decision": decision})
    resp = routes_agent.send_message(thread_id or env.thread_id, body, current_user=env.user, db=env.db)

    async def collect():
        return [c async for c in resp.body_iterator]
    import asyncio
    chunks = asyncio.run(collect())
    env.db.expire_all()
    return [json.loads(c[len("data: "):]) for c in chunks if c.startswith("data: ")]


def _notes(env):
    return env.db.get(Candidate, env.seed.candidate_id).flex_notes or {}


def test_confirm_runs_the_stored_args_and_marks_the_ticket_approved(env):
    fake = testing.FakeLLM([testing.text("Saved. New emails will use it.")]).install(env.monkeypatch)
    frames = _decide(env, "approve")
    types = [f["type"] for f in frames]
    assert types[:4] == ["run_start", "tool_start", "tool_end", "confirmation"]
    end = frames[2]
    assert end["ok"] is True and end["invalidate"] == ["profile"] and end["tool"] == "update_profile_notes"
    assert frames[3]["confirmation"]["status"] == "approved"
    assert frames[-1]["type"] == "done" and "profile" in frames[-1]["invalidate"]
    assert _notes(env)["why_now"] == "Graduating in May and want a fintech role"
    assert _notes(env)["best_project"] == "Built Splitkart, used by 1,200 students"
    assert env.db.get(AgentConfirmation, env.ticket_id).status == "approved"
    assert fake.calls == 1 and fake.seen[0]["tools"] is None
    assert fake.seen[0]["messages"][-1]["role"] == "system"
    user_msg = fake.seen[0]["messages"][-2]
    assert user_msg == {"role": "user", "content": "Confirm"}


def test_cancel_changes_nothing_and_marks_the_ticket_declined(env):
    testing.FakeLLM([testing.text("Okay, I left it as it was.")]).install(env.monkeypatch)
    frames = _decide(env, "decline")
    assert [f["type"] for f in frames if f["type"] in ("tool_start", "confirmation")] == ["confirmation"]
    assert frames[[f["type"] for f in frames].index("confirmation")]["confirmation"]["status"] == "declined"
    assert "why_now" not in _notes(env)
    assert env.db.get(AgentConfirmation, env.ticket_id).status == "declined"


def test_a_decided_ticket_cannot_be_used_again(env):
    testing.FakeLLM([testing.text("Saved.")]).install(env.monkeypatch)
    _decide(env, "approve")
    with pytest.raises(HTTPException) as e:
        _decide(env, "approve", turn="turn-c002")
    assert e.value.status_code == 409 and e.value.detail["code"] == "wrong_state"
    assert e.value.detail["message"] == "That request expired. Ask me again."


def test_an_expired_ticket_is_a_409_before_anything_streams(env):
    env.db.query(AgentConfirmation).filter(AgentConfirmation.id == env.ticket_id).update(
        {"expires_at": datetime.utcnow() - timedelta(minutes=1)})
    env.db.commit()
    with pytest.raises(HTTPException) as e:
        _decide(env, "approve")
    assert e.value.detail["code"] == "wrong_state" and env.started == []
    env.db.expire_all()
    assert env.db.get(AgentConfirmation, env.ticket_id).status == "expired"
    assert "why_now" not in _notes(env)


def test_a_ticket_from_another_thread_is_refused(env):
    other = store.create_thread(env.db, env.seed.user_id)
    env.db.commit()
    with pytest.raises(HTTPException) as e:
        _decide(env, "approve", thread_id=other.id)
    assert e.value.detail["code"] == "wrong_state" and env.started == []


def test_a_failing_action_marks_the_ticket_failed_and_still_replies(env):
    from services.agent import actions

    def broken(*a, **k):
        raise RuntimeError("database is down")
    env.monkeypatch.setattr(actions, "update_notes", broken)
    testing.FakeLLM([testing.text("That didn't save. Try again?")]).install(env.monkeypatch)
    frames = _decide(env, "approve")
    end = next(f for f in frames if f["type"] == "tool_end")
    assert end["ok"] is False and end["invalidate"] == []
    conf = next(f for f in frames if f["type"] == "confirmation")
    assert conf["confirmation"]["status"] == "failed"
    assert frames[-1]["type"] == "done"
    assert "database is down" not in str(frames)


def test_run_turn_with_a_confirmation_uses_its_own_queue(env):
    testing.FakeLLM([testing.text("Okay.")]).install(env.monkeypatch)
    run_id, _, _ = testing.start_turn(env.db, env.seed.user_id, "", thread_id=env.thread_id,
                                      confirmation={"id": env.ticket_id, "decision": "decline"})
    q = queue.Queue()
    runtime.run_turn(run_id, q, None, confirmation={"id": env.ticket_id, "decision": "decline"})
    assert testing.drain(q)[-1]["type"] == "done"
