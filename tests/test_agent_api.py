"""The student agent's HTTP surface (contract section 3).

Pins: all 29 routes exist under /api/v1/agent and answer 401 without a
session; bootstrap with no candidate is needs_resume with one suggestion;
bootstrap carries no lead address; the stream is text/event-stream with the
no-buffering headers and ends with done; GET /runs/{id} replays the persisted
frames after a seq and the final message; cancel is idempotent; malformed or
foreign ids are 404; an outreach thread needs an outreach the student owns.
"""

import asyncio
import json
import queue
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api import routes_agent
from core.config import settings
from services.agent import events, gating, runtime, store, testing
from services.agent.schemas import CreateThread, SendMessage

ROUTES = [
    ("GET", "/bootstrap"), ("GET", "/status"), ("GET", "/matches"), ("GET", "/matches/1"),
    ("POST", "/matches/1/skip"), ("GET", "/outreach"), ("GET", "/outreach/d_1"), ("POST", "/outreach/drafts"),
    ("POST", "/outreach/d_1/revise"), ("PATCH", "/outreach/d_1"), ("POST", "/outreach/d_1/approve"),
    ("POST", "/outreach/approve-all"), ("POST", "/outreach/d_1/discard"), ("POST", "/outreach/d_1/restore"),
    ("GET", "/replies"), ("GET", "/profile"), ("PATCH", "/profile/notes"), ("GET", "/memories"),
    ("POST", "/memories"), ("DELETE", "/memories/1"), ("GET", "/settings"), ("PATCH", "/settings"),
    ("GET", "/threads"), ("POST", "/threads"), ("GET", "/threads/1"), ("DELETE", "/threads/1"),
    ("POST", "/threads/1/messages"), ("GET", "/runs/1"), ("POST", "/runs/1/cancel"),
]


def test_every_agent_route_exists_and_needs_a_session():
    from api.main import app

    client = TestClient(app)
    for method, path in ROUTES:
        res = client.request(method, "/api/v1/agent" + path, json={})
        assert res.status_code == 401, (method, path, res.status_code)


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
    # Run the turn on the test thread so nothing depends on timing.
    monkeypatch.setattr(routes_agent, "_start_worker", lambda run_id, q, slot, umid, conf: runtime.run_turn(
        run_id, q, slot, user_message_id=umid, confirmation=conf))
    user = SimpleNamespace(id=seed.user_id, email=seed.user_email, name=seed.name)
    yield SimpleNamespace(db=db, seed=seed, user=user, monkeypatch=monkeypatch)
    db.close()
    gating.reset_slots_for_tests()


def _body(resp):
    return json.loads(resp.body)


def _drain(resp) -> list[str]:
    async def collect():
        return [c if isinstance(c, str) else c.decode() async for c in resp.body_iterator]
    return asyncio.run(collect())


def test_bootstrap_for_a_student_without_a_resume_is_needs_resume(env):
    db = env.db
    from database.models import User
    from datetime import datetime
    now = datetime.utcnow()
    db.add(User(id="u2", email="u2@example.com", name="New Student", email_verified=True,
                created_at=now, updated_at=now))
    db.commit()
    out = _body(routes_agent.bootstrap(current_user=SimpleNamespace(id="u2", email="u2@example.com",
                                                                    name="New Student"), db=db))
    assert out["enabled"] is True and out["status"]["state"] == "needs_resume"
    assert out["top_matches"] == [] and out["outreach"]["items"] == []
    assert len(out["suggestions"]) == 1
    assert out["thread"]["thread"]["scope"] == "main" and out["thread"]["messages"] == []


def test_bootstrap_has_real_data_and_no_lead_address(env):
    resp = routes_agent.bootstrap(current_user=env.user, db=env.db)
    out = _body(resp)
    assert out["enabled"] is True and out["user"]["first_name"] == "Asha"
    assert len(out["top_matches"]) == 5 and out["status_labels"]["scheduled"] == "Approved"
    blob = resp.body.decode()
    for address in env.seed.addresses:
        assert address not in blob
    assert "greeting" in out and out["thread"]["latest_run"] is None
    again = _body(routes_agent.bootstrap(current_user=env.user, db=env.db))
    assert again["thread"]["thread"]["id"] == out["thread"]["thread"]["id"]


def test_the_stream_is_unbuffered_sse_and_ends_with_done(env):
    testing.FakeLLM([testing.text("Hello Asha.")]).install(env.monkeypatch)
    thread = store.create_thread(env.db, env.seed.user_id)
    env.db.commit()
    resp = routes_agent.send_message(thread.id, SendMessage(content="Hi", client_turn_id="turn-0001"),
                                     current_user=env.user, db=env.db)
    assert resp.media_type == "text/event-stream"
    assert resp.headers["cache-control"] == "no-cache" and resp.headers["x-accel-buffering"] == "no"
    chunks = _drain(resp)
    frames = [json.loads(c[len("data: "):]) for c in chunks if c.startswith("data: ")]
    assert all(c.endswith("\n\n") for c in chunks)
    assert frames[0]["type"] == "run_start" and frames[-1]["type"] == "done"
    poll = _body(routes_agent.poll_run(frames[0]["run_id"], after_seq=0, current_user=env.user, db=env.db))
    assert poll["run"]["status"] == "done" and poll["message"]["content"] == "Hello Asha."
    assert [e["seq"] for e in poll["events"]] == [f["seq"] for f in frames if f["type"] != "text"]
    later = _body(routes_agent.poll_run(frames[0]["run_id"], after_seq=frames[-2]["seq"],
                                        current_user=env.user, db=env.db))
    assert [e["type"] for e in later["events"]] == ["done"]


def test_the_drain_pings_while_the_worker_is_quiet():
    q = queue.Queue()
    gen = events.drain(q, 0.01)
    assert next(gen) == ": ping\n\n"
    q.put({"type": "done"})
    q.put(None)
    assert next(gen).startswith("data: ")
    with pytest.raises(StopIteration):
        next(gen)


def test_cancel_is_idempotent_and_reports_the_status(env):
    run_id, _, _ = testing.start_turn(env.db, env.seed.user_id, "Hi")
    first = _body(routes_agent.cancel_run(run_id, current_user=env.user, db=env.db))
    second = _body(routes_agent.cancel_run(run_id, current_user=env.user, db=env.db))
    assert first == second == {"ok": True, "status": "running"}
    testing.FakeLLM([testing.text("never")]).install(env.monkeypatch)
    q = queue.Queue()
    runtime.run_turn(run_id, q, None)
    assert testing.drain(q)[-1]["status"] == "cancelled"
    assert _body(routes_agent.cancel_run(run_id, current_user=env.user, db=env.db))["status"] == "cancelled"


def test_a_malformed_or_foreign_id_is_a_404(env):
    for bad in ("x_1", "d_0", "d_abc", "e_"):
        with pytest.raises(HTTPException) as e:
            routes_agent.get_outreach(bad, current_user=env.user, db=env.db)
        assert e.value.status_code == 404 and e.value.detail["code"] == "not_found"
    other = testing.seed_student(env.db, user_id="u9", matches=2, with_email=0, campaign=False)
    t = store.create_thread(env.db, other.user_id)
    env.db.commit()
    with pytest.raises(HTTPException) as e:
        routes_agent.get_thread(t.id, before=None, limit=50, current_user=env.user, db=env.db)
    assert e.value.status_code == 404
    run_id, _, _ = testing.start_turn(env.db, other.user_id, "Hi")
    with pytest.raises(HTTPException) as e:
        routes_agent.poll_run(run_id, after_seq=0, current_user=env.user, db=env.db)
    assert e.value.status_code == 404


def test_an_outreach_thread_needs_an_outreach_the_student_owns(env):
    with pytest.raises(HTTPException) as e:
        routes_agent.create_thread(CreateThread(scope="outreach:e_999999"), current_user=env.user, db=env.db)
    assert e.value.status_code == 404
    oid = f"e_{env.seed.email_ids[0]}"
    first = _body(routes_agent.create_thread(CreateThread(scope=f"outreach:{oid}"), current_user=env.user, db=env.db))
    again = _body(routes_agent.create_thread(CreateThread(scope=f"outreach:{oid}"), current_user=env.user, db=env.db))
    assert first["id"] == again["id"] and first["scope"] == f"outreach:{oid}"


def test_threads_list_and_archive(env):
    t = _body(routes_agent.create_thread(CreateThread(title="Plans"), current_user=env.user, db=env.db))
    items = _body(routes_agent.list_threads(scope=None, current_user=env.user, db=env.db))["items"]
    assert items[0]["id"] == t["id"] and items[0]["title"] == "Plans"
    assert _body(routes_agent.delete_thread(t["id"], current_user=env.user, db=env.db)) == {"ok": True}
    items = _body(routes_agent.list_threads(scope=None, current_user=env.user, db=env.db))["items"]
    assert all(i["id"] != t["id"] for i in items)
