"""Pins services/agent/reads.py: outreach rows and counts from drafts and
Touch 1 emails, match states and the never-contact filter, replies grouped,
status and greeting from real counts, the no-resume state, and that no lead
address appears in any read the workspace or the model gets."""

from types import SimpleNamespace

import pytest

from services.agent import actions, reads, store
from services.agent.context import ActionError
from services.agent.testing import (
    inline_drafting, make_engine, patch_writer, seed_student, session_factory, use_engine,
)


@pytest.fixture()
def env(monkeypatch):
    engine = make_engine()
    use_engine(monkeypatch, engine)
    db = session_factory(engine)()
    seed = seed_student(db, with_email=3)
    patch_writer(monkeypatch)
    inline_drafting(monkeypatch)
    yield SimpleNamespace(db=db, seed=seed)
    db.close()


def test_outreach_merges_touch_one_emails_with_drafts(env):
    db, s = env.db, env.seed
    res = actions.start_drafts(db, s.user_id, [s.lead_ids[5]])
    db.commit()
    actions.submit_drafts(res)
    out = reads.outreach_list(db, s.user_id, status="all")
    words = {r.id: (r.status_word, r.note) for r in out.items}
    assert words[f"e_{s.email_ids[0]}"] == ("replied", "They wrote back.")
    assert words[f"e_{s.email_ids[1]}"] == ("sent", "Sent. Follow-up planned.")
    assert words[f"e_{s.email_ids[2]}"] == ("scheduled", "Waiting for its send slot.")
    assert words[res.items[0].id] == ("needs_you", "Draft ready. Review it.")
    assert out.counts.model_dump() == {"all": 4, "needs_you": 1, "scheduled": 1, "sent": 1, "replied": 1,
                                       "failed": 0, "skipped": 0}
    only = reads.outreach_list(db, s.user_id, status="needs_you")
    assert only.total == 1 and only.counts.all == 4
    sent = reads.outreach_detail(db, s.user_id, f"e_{s.email_ids[1]}")
    assert [t.number for t in sent.thread.touches] == [0, 1] and not sent.can_approve


def test_matches_carry_state_and_never_contact_is_filtered(env):
    db, s = env.db, env.seed
    store.update_settings(db, s.user_id, never_contact=[{"kind": "person", "value": "priya nair"}])
    cards = {c.lead_id: c for c in reads.ranked_matches(db, s.user_id, limit=50).items}
    assert s.lead_ids[2] not in cards
    assert cards[s.lead_ids[0]].state == "contacted" and cards[s.lead_ids[3]].state == "new"
    top = reads.top_matches(db, s.user_id)
    assert len(top) == 5 and all(c.state == "new" for c in top) and s.lead_ids[2] not in [c.lead_id for c in top]
    strong = reads.ranked_matches(db, s.user_id, strong_only=True, limit=50)
    assert all(c.strong for c in strong.items) and strong.total == strong.strong_total


def test_replies_are_grouped(env):
    out = reads.replies(env.db, env.seed.user_id)
    assert out.counts.positive == 1 and out.items[0].group == "positive"
    assert out.items[0].reply_text == "Happy to chat next week."
    assert reads.replies(env.db, env.seed.user_id, group="bounced").total == 0


def test_status_and_greeting_use_real_counts(env):
    db, s = env.db, env.seed
    st = reads.status(db, s.user_id)
    assert st.state == "campaign_active" and st.sending_enabled is False
    assert (st.matches.total, st.matches.strong, st.matches.new) == (12, 10, 9)
    assert st.counts.all == 3 and st.credits.available == 10 and st.gmail.connected
    assert reads.greeting(db, s.user_id, st).startswith("Hi Asha. You have 9 people")
    assert [x.label for x in reads.default_suggestions(st)] == ["Write to my top 5", "Why these people?",
                                                               "Where are my emails?"]


def test_a_student_with_no_resume(env):
    db = env.db
    seed_student(db, user_id="fresh", matches=0, campaign=False)
    from database.models import Candidate
    db.query(Candidate).filter(Candidate.user_id == "fresh").delete()
    db.commit()
    st = reads.status(db, "fresh")
    assert st.state == "needs_resume" and st.candidate_id is None
    assert len(reads.default_suggestions(db, "fresh")) == 1
    assert reads.ranked_matches(db, "fresh").total == 0
    with pytest.raises(ActionError):
        reads.profile(db, "fresh")


def test_no_read_carries_a_lead_address(env):
    db, s = env.db, env.seed
    blobs = [reads.ranked_matches(db, s.user_id, limit=50).model_dump_json(),
             reads.match_detail(db, s.user_id, s.lead_ids[0]).model_dump_json(),
             reads.outreach_list(db, s.user_id).model_dump_json(),
             reads.outreach_detail(db, s.user_id, f"e_{s.email_ids[0]}").model_dump_json(),
             reads.replies(db, s.user_id).model_dump_json(),
             reads.profile(db, s.user_id).model_dump_json()]
    for blob in blobs:
        for address in s.addresses:
            assert address not in blob
    detail = reads.match_detail(db, s.user_id, s.lead_ids[0])
    assert detail.has_address is True and detail.why.source == "ai"
