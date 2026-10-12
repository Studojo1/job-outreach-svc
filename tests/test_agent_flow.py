"""A real agent turn end to end, with only the model and the email writer faked.

runtime.run_turn runs on the test thread against in-memory SQLite, so frames
are read straight off the queue. Pins the stream order (contract section 4),
resume from persisted events, cancel, a model failure, drafting and revising
with the generator mocked, and the Phase 1 guardrails as behaviour: the model
never sees an address, nothing is sent or charged, no vendor is called. Also
the outreach status table (contract section 1).
"""
import json
import queue
from types import SimpleNamespace

import pytest

from database.models import AgentDraft, AgentMessage, AgentRun, Campaign, CreditLedger, EmailSent, UserCredit
from services.agent import store
from services.agent.testing import (
    FakeLLM, drain, enable_agent, fail, inline_drafting, make_engine, patch_writer, seed_student, session_factory, start_turn,
    text, tool, use_engine,
)


def _require(*modules):
    import importlib
    try:
        return [importlib.import_module(m) for m in modules]
    except ImportError as e:
        pytest.skip(f"not importable yet: {e}")


@pytest.fixture()
def env(monkeypatch):
    (runtime,) = _require("services.agent.runtime")
    _require("services.agent.tools", "services.agent.drafting")
    engine = make_engine()
    use_engine(monkeypatch, engine)
    enable_agent(monkeypatch)
    calls = []

    def no_network(*a, **k):
        calls.append(a[1:3] if len(a) > 2 else a)
        raise AssertionError("the agent made a network call")

    import requests
    monkeypatch.setattr(requests.Session, "request", no_network)
    try:
        import httpx
        monkeypatch.setattr(httpx.Client, "send", no_network)
    except ImportError:
        pass
    db = session_factory(engine)()
    seed = seed_student(db, matches=10, with_email=2)
    other = seed_student(db, user_id="other", matches=3, with_email=1)
    writes = patch_writer(monkeypatch)
    inline_drafting(monkeypatch)
    yield SimpleNamespace(db=db, seed=seed, other=other, runtime=runtime, writes=writes, network=calls,
                          monkeypatch=monkeypatch)
    db.close()


def _turn(env, script, content="Hi", **kw):
    fake = FakeLLM(script).install(env.monkeypatch)
    run_id, msg_id, thread_id = start_turn(env.db, env.seed.user_id, content, **kw)
    q = queue.Queue()
    env.runtime.run_turn(run_id, q, None)
    env.db.expire_all()
    return SimpleNamespace(run_id=run_id, thread_id=thread_id, msg_id=msg_id, frames=drain(q), fake=fake)


def _types(frames):
    return [f["type"] for f in frames]


def _no_text(frames):
    return [f for f in frames if f["type"] != "text"]


# ---------------------------------------------------------------- the stream

def test_text_only_turn_streams_in_order_and_persists_all_but_text(env):
    t = _turn(env, [text("Your top match is Ananya at Company0.")])
    kinds = _types(t.frames)
    assert kinds[0] == "run_start" and kinds[-2:] == ["message_end", "done"]
    assert set(kinds[1:-2]) <= {"text", "suggestions"} and "text" in kinds
    assert [f["seq"] for f in t.frames] == list(range(1, len(t.frames) + 1))
    assert t.frames[0]["run_id"] == t.run_id and t.frames[0]["user_message_id"] == t.msg_id
    end, done = t.frames[-2], t.frames[-1]
    assert end["content"] == "Your top match is Ananya at Company0." and done["status"] == "done"
    assert "".join(f["delta"] for f in t.frames if f["type"] == "text") == end["content"]
    assert store.events_after(env.db, t.run_id, env.seed.user_id, 0) == _no_text(t.frames)
    assert store.get_run(env.db, t.run_id, env.seed.user_id).status == "done"
    assert store.final_message(env.db, t.run_id, env.seed.user_id).content == end["content"]


def test_tool_block_is_paired_and_the_model_sees_the_result(env):
    t = _turn(env, [tool("get_status", {}, "c1"), text("You have 10 matches.")])
    blocks = [f for f in t.frames if f.get("call_id") == "c1"]
    assert blocks[0]["type"] == "tool_start" and blocks[-1]["type"] == "tool_end" and blocks[-1]["ok"] is True
    assert {f["type"] for f in blocks[1:-1]} <= {"card"}
    assert _types(t.frames)[-2:] == ["message_end", "done"]
    second = t.fake.seen[1]["messages"]
    results = [m for m in second if m.get("role") == "tool" and m.get("tool_call_id") == "c1"]
    assert len(results) == 1 and "sending_enabled" in results[0]["content"]
    assert t.fake.seen[0]["tools"] and {x["function"]["name"] for x in t.fake.seen[0]["tools"]} >= {"get_status"}


def test_resume_after_any_seq_replays_the_rest(env):
    t = _turn(env, [tool("list_matches", {"limit": 3}, "c1"), text("Three people.")])
    persisted = _no_text(t.frames)
    mid = persisted[1]["seq"]
    assert store.events_after(env.db, t.run_id, env.seed.user_id, mid) == [f for f in persisted if f["seq"] > mid]


def test_cancel_before_step_two_ends_cancelled_without_another_model_call(env):
    def step_one(messages, tools):
        other = session_factory(env.db.get_bind())()
        run = other.query(AgentRun).order_by(AgentRun.id.desc()).first()
        store.request_cancel(other, run.id, env.seed.user_id)
        other.close()
        return [text("Looking now."), tool("get_status", {}, "c1")]

    t = _turn(env, [step_one, text("never said")])
    assert t.frames[-1]["type"] == "done" and t.frames[-1]["status"] == "cancelled"
    assert t.fake.calls == 1
    assert store.get_run(env.db, t.run_id, env.seed.user_id).status == "cancelled"


def test_model_failure_ends_with_one_error_and_no_assistant_message(env):
    t = _turn(env, [fail()])
    assert _types(t.frames) == ["run_start", "error"]
    assert t.frames[-1]["code"] == "model_unavailable" and t.frames[-1]["retryable"] is True
    assert store.get_run(env.db, t.run_id, env.seed.user_id).status == "error"
    assert env.db.query(AgentMessage).filter(AgentMessage.run_id == t.run_id,
                                             AgentMessage.role == "assistant").count() == 0


# ---------------------------------------------------------------- drafting with the writer mocked

def test_draft_then_revise(env):
    leads = env.seed.lead_ids[2:5]
    t = _turn(env, [tool("draft_emails", {"lead_ids": leads}, "c1"), text("3 drafts are in Home under Needs you.")])
    drafts = [f for f in t.frames if f["type"] == "draft"]
    assert len(drafts) == 3 and [w[0] for w in env.writes].count("generate") == 3
    for f in drafts:
        o = f["outreach"]
        assert (o["status_word"], o["server_status"], o["version"], o["kind"]) == ("needs_you", "needs_review", 1,
                                                                                   "draft")
        assert f["previous"] is None and o["sending_enabled"] is False
    end = [f for f in t.frames if f["type"] == "tool_end"][0]
    assert {"outreach", "matches", "status"} <= set(end["invalidate"])
    rows = env.db.query(AgentDraft).filter(AgentDraft.user_id == env.seed.user_id).all()
    assert sorted(r.lead_id for r in rows) == sorted(leads) and {r.status for r in rows} == {"needs_review"}

    target = drafts[0]["outreach"]["id"]
    r = _turn(env, [tool("revise_draft", {"outreach_id": target, "instruction": "Make it shorter."}, "c2"),
                    text("Shorter now.")], thread_id=t.thread_id)
    revised = [f for f in r.frames if f["type"] == "draft"]
    assert len(revised) == 1 and revised[0]["outreach"]["version"] == 2
    assert revised[0]["previous"]["version"] == 1


# ---------------------------------------------------------------- Phase 1 guardrails as behaviour

def _counts(db, user_id):
    return (db.query(EmailSent).count(), db.query(Campaign).count(), db.query(CreditLedger).count(),
            db.query(UserCredit).filter(UserCredit.user_id == user_id).one().used_credits)


def test_a_busy_turn_never_shows_an_address_sends_spends_or_calls_out(env):
    s = env.seed
    before = _counts(env.db, s.user_id)
    script = [
        [tool("get_profile", {}, "a1"), tool("get_status", {}, "a2"), tool("list_matches", {"limit": 25}, "a3")],
        [tool("get_match", {"lead_id": s.lead_ids[0]}, "a4"), tool("list_outreach", {}, "a5"),
         tool("get_outreach", {"outreach_id": f"e_{s.email_ids[0]}"}, "a6")],
        [tool("send_email", {"to": "anyone"}, "a7"), tool("draft_emails", {"lead_ids": s.lead_ids[:2]}, "a8")],
        [tool("get_match", {"lead_id": env.other.lead_ids[0]}, "a9")],
        text("Two drafts are waiting for you. Nothing is sent until you approve it."),
    ]
    t = _turn(env, script, "Send my emails and find me more people")
    assert t.frames[-1]["type"] == "done"
    model_saw, ui_got = json.dumps(t.fake.seen), json.dumps(t.frames)
    for address in s.addresses | env.other.addresses:
        assert address not in model_saw and address not in ui_got, address
    # The student's own mailbox may reach the UI (Status.gmail.address), never the model.
    for own in (s.gmail_address, s.user_email):
        assert own not in model_saw, own
    tool_msgs = {m["tool_call_id"]: m["content"] for m in t.fake.seen[-1]["messages"] if m.get("role") == "tool"}
    assert tool_msgs["a7"].startswith("REFUSED: no such tool")
    assert tool_msgs["a9"].startswith("NOT_FOUND")
    assert _counts(env.db, s.user_id) == before
    assert env.network == []


# ---------------------------------------------------------------- the status table (contract section 1)

DRAFT_TABLE = [
    ("drafting", [], "needs_you", "Writing your email.", True),
    ("needs_review", [], "needs_you", "Draft ready. Review it.", False),
    ("needs_review", [{"text": "1,200", "reason": "check"}], "needs_you", "Draft ready. Check the marked line.", False),
    ("approved", [], "scheduled", "Approved. Sending comes next, so nothing has gone out.", False),
    ("skipped", [], "skipped", "You passed on this person.", False),
    ("failed", [], "failed", "I couldn't write this one. Try again.", False),
    ("discarded", [], None, None, None), ("superseded", [], None, None, None), ("sent", [], None, None, None),
]
EMAIL_TABLE = [
    ("pending_enrichment", [], "scheduled", "Waiting for its send slot."),
    ("queued", [], "scheduled", "Waiting for its send slot."),
    ("sending", [], "scheduled", "Waiting for its send slot."),
    ("sent", [], "sent", "Sent."),
    ("sent", ["followup_pending"], "sent", "Sent. Follow-up planned."),
    ("sent", ["queued"], "sent", "Sent. Follow-up planned."),
    ("sent", ["cancelled_reply"], "sent", "Sent."),
    ("replied", [], "replied", "They wrote back."),
    ("bounced", [], "failed", "This address did not accept the email."),
    ("failed", [], "failed", "This one could not be sent."),
    ("generation_failed", [], "failed", "This one could not be sent."),
    ("expired", [], "failed", "Not sent before the campaign ended. The credit came back to you."),
    ("paused", [], None, None), ("cancelled", [], None, None),
]
_LEAD = SimpleNamespace(id=7, name="Ananya Rao", title="Engineering Lead", company="Kitebase",
                        company_domain="kitebase.in", email="hidden@kitebase.in")


@pytest.mark.parametrize("status,flags,word,note,busy", DRAFT_TABLE)
def test_draft_status_words(status, flags, word, note, busy):
    (sw,) = _require("services.agent.status_words")
    row = sw.draft_row(AgentDraft(id=88, lead_id=7, status=status, subject="s", body="b", version=1, flags=flags),
                       _LEAD)
    if word is None:
        assert row is None
        return
    assert (row.id, row.status_word, row.note, row.busy) == ("d_88", word, note, busy)
    assert "hidden@" not in row.model_dump_json()


@pytest.mark.parametrize("status,followups,word,note", EMAIL_TABLE)
def test_email_status_words(status, followups, word, note):
    (sw,) = _require("services.agent.status_words")
    email = EmailSent(id=4512, lead_id=7, status=status, subject="s", body="b", followup_number=0,
                      to_email="hidden@kitebase.in")
    fus = [EmailSent(id=9000 + i, status=f, followup_number=i + 1) for i, f in enumerate(followups)]
    row = sw.email_row(email, _LEAD, fus)
    if word is None:
        assert row is None
        return
    assert (row.id, row.status_word, row.note) == ("e_4512", word, note)
    assert "hidden@" not in row.model_dump_json()


@pytest.mark.parametrize("status,sentiment,group", [
    ("replied", "positive", "positive"), ("replied", "negative", "not_now"), ("replied", "neutral", "question"),
    ("replied", None, "question"), ("bounced", None, "bounced"), ("sent", None, None)])
def test_reply_groups(status, sentiment, group):
    (sw,) = _require("services.agent.status_words")
    assert sw.reply_group(EmailSent(status=status, reply_sentiment=sentiment)) == group
