"""The student agent end to end over real HTTP (integration pass).

Unlike the other agent tests, nothing here calls a route function directly:
the full `api.main` app (with its logging and SSE-safe gzip middleware) runs
behind TestClient, the turn runs on the real worker thread the route starts,
and the database is a file SQLite so every thread gets its own connection.
The model is the FakeLLM and the email writer is faked, so nothing leaves the
process. Pins: a turn streams the contract's frames in order with contiguous
seq and is not gzipped; GET /runs replays it after any seq; a repeated
client_turn_id is a 409; every workspace mutation route reaches its action
with matching arguments and returns its schema; a confirmation turn executes
the stored args; no emails_sent row is ever written.
"""

import json

import pytest
from sqlalchemy import create_engine, func

from core.config import settings
from database.models import Base, EmailSent, User
from services.agent import gating, testing

B = "/api/v1/agent"
PC = {"route": "home", "outreach_id": None, "lead_id": None, "filter": None, "query": None, "selected": []}


@pytest.fixture
def env(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from api.dependencies import get_current_user
    from api.main import app
    from database.session import get_db

    engine = create_engine(f"sqlite:///{tmp_path / 'agent.db'}",
                           connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine, tables=[t for t in Base.metadata.sorted_tables if testing._sqlite_ok(t)])
    factory = testing.use_engine(monkeypatch, engine)
    testing.enable_agent(monkeypatch)
    testing.inline_drafting(monkeypatch)
    writes = testing.patch_writer(monkeypatch)
    monkeypatch.setattr(settings, "AGENT_DRAFT_WAIT_SECONDS", 20)
    gating.reset_slots_for_tests()
    db = factory()
    seed = testing.seed_student(db)
    db.close()

    def _db():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    def _user():
        s = factory()
        try:
            u = s.get(User, seed.user_id)
            s.expunge(u)
            return u
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _user
    client = TestClient(app)
    try:
        yield type("Env", (), {"client": client, "seed": seed, "factory": factory, "writes": writes,
                               "monkeypatch": monkeypatch})
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
        gating.reset_slots_for_tests()
        engine.dispose()


def _emails(env) -> int:
    s = env.factory()
    try:
        return s.query(func.count(EmailSent.id)).scalar()
    finally:
        s.close()


def _turn(env, thread_id: int, body: dict) -> tuple:
    with env.client.stream("POST", f"{B}/threads/{thread_id}/messages", json=body,
                           headers={"Accept-Encoding": "gzip"}) as r:
        frames = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
        return r, frames


def _check_order(frames: list) -> None:
    types = [f["type"] for f in frames]
    assert types[0] == "run_start" and types[-2:] == ["message_end", "done"]
    assert [f["seq"] for f in frames] == list(range(1, len(frames) + 1))
    open_calls = []
    for f in frames[1:-2]:
        if f["type"] == "tool_start":
            assert not open_calls
            open_calls.append(f["call_id"])
        elif f["type"] == "tool_end":
            assert open_calls == [f["call_id"]]
            open_calls.pop()
        elif f["type"] in ("card", "draft"):
            assert open_calls == [f["call_id"]]
        else:
            assert f["type"] in ("text", "confirmation", "navigate", "suggestions"), f["type"]
    assert not open_calls


def test_a_turn_streams_over_http_resumes_and_refuses_a_repeat(env):
    c, L = env.client, env.seed.lead_ids
    testing.FakeLLM([
        [testing.text("Looking at your top matches."), testing.tool("list_matches", {"limit": 5}, "c1")],
        testing.tool("draft_emails", {"lead_ids": [L[2], L[3], L[4]]}, "c2"),
        testing.text("3 drafts are in Home under Needs you."),
    ]).install(env.monkeypatch)
    boot = c.get(f"{B}/bootstrap").json()
    tid = boot["thread"]["thread"]["id"]
    r, frames = _turn(env, tid, {"content": "Write to my top people", "client_turn_id": "turn-e2e-0001",
                                 "page_context": PC})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert "content-encoding" not in r.headers
    assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"
    _check_order(frames)
    assert sum(f["type"] == "draft" for f in frames) == 3
    end = next(f for f in frames if f["type"] == "tool_end" and f["call_id"] == "c2")
    assert end["ok"] and end["invalidate"] == ["outreach", "matches", "status"]
    assert frames[-1]["status"] == "done" and frames[-1]["invalidate"] == ["outreach", "matches", "status"]
    assert not any(a in json.dumps(frames) for a in env.seed.addresses)

    run_id = frames[0]["run_id"]
    poll = c.get(f"{B}/runs/{run_id}", params={"after_seq": 0}).json()
    assert poll["run"]["status"] == "done" and poll["message"]["content"] == frames[-2]["content"]
    assert [e["seq"] for e in poll["events"]] == [f["seq"] for f in frames if f["type"] != "text"]
    tail = c.get(f"{B}/runs/{run_id}", params={"after_seq": frames[-3]["seq"]}).json()
    assert [e["type"] for e in tail["events"]] == ["message_end", "done"]

    again = c.post(f"{B}/threads/{tid}/messages", json={"content": "again", "client_turn_id": "turn-e2e-0001"})
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "duplicate_turn" and again.json()["detail"]["run_id"] == run_id
    needs = c.get(f"{B}/outreach", params={"status": "needs_you"}).json()
    assert sum(i["kind"] == "draft" and i["server_status"] == "needs_review" for i in needs["items"]) == 3
    detail = c.get(f"{B}/threads/{tid}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["latest_run"]["status"] == "done"
    assert _emails(env) == 4


def test_every_workspace_mutation_route_round_trips(env):
    c, L = env.client, env.seed.lead_ids
    before = _emails(env)
    made = c.post(f"{B}/outreach/drafts", json={"lead_ids": [L[2], L[3], L[4]]})
    assert made.status_code == 202, made.text
    ids = [row["id"] for row in made.json()["items"]]
    assert len(ids) == 3 and all(i.startswith("d_") for i in ids)
    d1, d2, d3 = ids
    assert c.get(f"{B}/outreach/{d1}").json()["server_status"] == "needs_review"

    edited = c.patch(f"{B}/outreach/{d1}", json={"subject": "my own subject", "expected_version": 1})
    assert edited.status_code == 200 and edited.json()["version"] == 2 and edited.json()["subject"] == "my own subject"
    revised = c.post(f"{B}/outreach/{d1}/revise", json={"instruction": "Shorter", "expected_version": 2})
    assert revised.status_code == 200, revised.text
    assert revised.json()["outreach"]["version"] == 3 and revised.json()["previous"]["version"] == 2
    approved = c.post(f"{B}/outreach/{d1}/approve", json={"expected_version": 3})
    assert approved.status_code == 200 and approved.json()["sent"] is False
    assert approved.json()["outreach"]["status_word"] == "scheduled"
    stale = c.post(f"{B}/outreach/{d2}/approve", json={"expected_version": 9})
    assert stale.status_code == 409 and stale.json()["detail"]["current"]["id"] == d2
    many = c.post(f"{B}/outreach/approve-all", json={"ids": [d1, d2]})
    assert many.status_code == 200 and [r["id"] for r in many.json()["approved"]] == [d2]
    assert many.json()["sent"] is False and len(many.json()["refused"]) == 1
    gone = c.post(f"{B}/outreach/{d3}/discard", json={})
    assert gone.status_code == 200 and gone.json()["server_status"] == "discarded"
    assert d3 not in [i["id"] for i in c.get(f"{B}/outreach", params={"status": "all"}).json()["items"]]

    skipped = c.post(f"{B}/matches/{L[6]}/skip", json={"reason": "not my field"})
    assert skipped.status_code == 200 and skipped.json()["status_word"] == "skipped"
    assert c.get(f"{B}/matches/{L[6]}").json()["state"] == "skipped"
    assert c.post(f"{B}/outreach/{skipped.json()['id']}/restore", json={}).json() == {"ok": True}
    assert c.get(f"{B}/matches/{L[6]}").json()["state"] == "new"

    notes = c.patch(f"{B}/profile/notes", json={"why_now": "Graduating in May"})
    assert notes.status_code == 200 and "Graduating in May" in notes.text
    mem = c.post(f"{B}/memories", json={"text": "Keep emails short", "scope": "emails"})
    assert mem.status_code == 200 and mem.json()["source"] == "settings"
    assert c.delete(f"{B}/memories/{mem.json()['id']}").json() == {"ok": True}
    assert c.get(f"{B}/memories").json()["items"] == []
    st = c.patch(f"{B}/settings", json={"review_before_send": False,
                                         "never_contact": [{"kind": "company", "value": "Company9"}]})
    assert st.status_code == 200 and st.json()["review_before_send"] is False
    th = c.post(f"{B}/threads", json={"scope": f"outreach:{d1}"})
    assert th.status_code == 200 and th.json()["scope"] == f"outreach:{d1}"
    assert c.delete(f"{B}/threads/{th.json()['id']}").json() == {"ok": True}
    assert c.get(f"{B}/replies").status_code == 200
    assert c.get(f"{B}/outreach/x_1").status_code == 404
    assert _emails(env) == before


def test_a_confirmation_turn_runs_the_stored_args_over_http(env):
    c = env.client
    tid = c.get(f"{B}/bootstrap").json()["thread"]["thread"]["id"]
    fake = testing.FakeLLM([testing.tool("update_profile_notes", {"credential": "Dean's list"}, "c1"),
                            testing.text("Press Confirm to save it.")]).install(env.monkeypatch)
    _, frames = _turn(env, tid, {"content": "Add my dean's list award", "client_turn_id": "turn-e2e-0101"})
    _check_order(frames)
    ticket = next(f["confirmation"] for f in frames if f["type"] == "confirmation")
    assert ticket["status"] == "pending" and "Dean's list" not in c.get(f"{B}/profile").text
    fake.script = [testing.text("Saved.")]
    _, frames = _turn(env, tid, {"content": "", "client_turn_id": "turn-e2e-0102",
                                 "confirmation": {"id": ticket["id"], "decision": "approve"}})
    _check_order(frames)
    assert next(f for f in frames if f["type"] == "tool_end")["invalidate"] == ["profile"]
    assert next(f for f in frames if f["type"] == "confirmation")["confirmation"]["status"] == "approved"
    assert "Dean's list" in c.get(f"{B}/profile").text
    reuse = c.post(f"{B}/threads/{tid}/messages", json={"content": "", "client_turn_id": "turn-e2e-0103",
                                                         "confirmation": {"id": ticket["id"], "decision": "approve"}})
    assert reuse.status_code == 409 and reuse.json()["detail"]["code"] == "wrong_state"
