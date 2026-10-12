"""Pins services/agent/drafting.py: a drafting row is written through the
existing generate_email_for_lead path (writer faked), instructions and email
preferences add one revise pass, dashes are stripped, facts and claim flags
are stored, writer failures end as `failed`, and wait() reports what is done."""

from types import SimpleNamespace

import pytest

from database.models import AgentDraft
from services.agent import actions, drafting, store
from services.agent.testing import make_engine, patch_writer, seed_student, session_factory, use_engine


@pytest.fixture()
def env(monkeypatch):
    engine = make_engine()
    use_engine(monkeypatch, engine)
    db = session_factory(engine)()
    seed = seed_student(db)
    yield SimpleNamespace(db=db, seed=seed, monkeypatch=monkeypatch)
    db.close()


def _start(env, lead_index=3, **kw):
    res = actions.start_drafts(env.db, env.seed.user_id, [env.seed.lead_ids[lead_index]], **kw)
    env.db.commit()
    return int(res.items[0].id[2:])


def _row(env, draft_id):
    env.db.expire_all()
    return env.db.get(AgentDraft, draft_id)


def test_fill_writes_version_one_with_facts_and_no_flags_for_true_claims(env):
    log = patch_writer(env.monkeypatch, body="Hi {first},\n\nI built Splitkart \u2014 used by 1,200 students.\n\nAsha")
    did = _start(env)
    assert drafting.fill(did) == "needs_review"
    row = _row(env, did)
    assert (row.status, row.version, row.edited_by, row.style) == ("needs_review", 1, "agent", "warm_intro")
    assert "\u2014" not in row.body and row.original_body == row.body and row.subject == "quick question Rohit"
    assert row.flags == [] and any(f["source"] == "notes" for f in row.facts_used)
    assert [w[0] for w in log] == ["generate"]


def test_an_invented_number_or_name_is_flagged(env):
    patch_writer(env.monkeypatch, body="Hi {first},\n\nI cut costs by 40% at Google last year.\n\nAsha")
    did = _start(env)
    drafting.fill(did)
    flags = [f["text"] for f in _row(env, did).flags]
    assert "40%" in flags and "Google" in flags


def test_instructions_and_email_memories_add_one_revise_pass(env):
    store.save_memory(env.db, env.seed.user_id, "Keep it under 80 words", scope="emails")
    env.db.commit()
    log = patch_writer(env.monkeypatch, revise={"subject": "quick question", "body": "Hi there,\n\nShort and "
                                                "true, I built Splitkart.\n\nAsha"})
    did = _start(env, instructions="Mention I am free in December")
    drafting.fill(did)
    kinds = [w[0] for w in log]
    assert kinds == ["generate", "revise"]
    prompt = log[1][1][0]
    assert "Mention I am free in December" in prompt and "Keep it under 80 words" in prompt
    assert _row(env, did).body.startswith("Hi there")


def test_a_writer_failure_marks_the_row_failed(env):
    patch_writer(env.monkeypatch, error=ValueError("writer down"))
    did = _start(env)
    assert drafting.fill(did) == "failed"
    row = _row(env, did)
    assert row.status == "failed" and row.failure_reason == "writer_failed"


def test_a_row_skipped_while_writing_is_left_alone(env):
    patch_writer(env.monkeypatch)
    did = _start(env)
    actions.skip_lead(env.db, env.seed.user_id, env.seed.lead_ids[3])
    env.db.commit()
    assert drafting.fill(did) is None
    assert _row(env, did).status == "skipped"


def test_wait_reports_finished_and_pending_rows(env):
    patch_writer(env.monkeypatch)
    done_id, pending_id = _start(env, 3), _start(env, 4)
    drafting.fill(done_id)
    seen = []
    done, pending = drafting.wait([done_id, pending_id], 0.2, on_done=seen.append)
    assert (done, pending, seen) == ([done_id], [pending_id], [done_id])


@pytest.mark.parametrize("body,expected", [
    ("Hi Ananya,\n\nI built Splitkart for 1,200 students.\n\nAsha", []),
    ("Hi Ananya,\n\nI led a team of 12 at Stripe.\n\nAsha", ["12", "Stripe"]),
    ("Thanks. Best, Asha", []),
])
def test_post_check(body, expected):
    corpus = "ananya splitkart 1,200 students asha"
    assert [f["text"] for f in drafting.post_check(body, corpus)] == expected
