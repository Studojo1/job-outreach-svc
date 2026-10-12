"""Pins services/agent/actions.py, the one function behind each workspace
button and tool: the refusal order of start_drafts, skip and restore, approve
(hash, version, never sent, retry safe), edit and revise clearing approval,
discard returning the lead to the matches, the profile notes merge, and that
nothing touches emails_sent or credits."""

from types import SimpleNamespace

import pytest

from database.models import AgentDraft, Candidate, CreditLedger, EmailSent, UserCredit
from services.agent import actions, drafting, reads, store
from services.agent.context import ActionError
from services.agent.schemas import UpdateNotes
from services.agent.testing import (
    inline_drafting, make_engine, patch_writer, seed_student, session_factory, use_engine,
)


@pytest.fixture()
def env(monkeypatch):
    engine = make_engine()
    use_engine(monkeypatch, engine)
    db = session_factory(engine)()
    seed = seed_student(db)
    other = seed_student(db, user_id="other", matches=3, with_email=0, campaign=False)
    writes = patch_writer(monkeypatch)
    inline_drafting(monkeypatch)
    yield SimpleNamespace(db=db, seed=seed, other=other, writes=writes)
    db.close()


def _draft(env, lead_index=3):
    db, s = env.db, env.seed
    res = actions.start_drafts(db, s.user_id, [s.lead_ids[lead_index]])
    db.commit()
    actions.submit_drafts(res)
    db.expire_all()
    return res.items[0].id


def test_start_drafts_refuses_in_the_contract_order(env, monkeypatch):
    db, s = env.db, env.seed
    store.update_settings(db, s.user_id, never_contact=[{"kind": "company", "value": "Company4"}])
    actions.skip_lead(db, s.user_id, s.lead_ids[5])
    first = actions.start_drafts(db, s.user_id, [s.lead_ids[6]])
    db.commit()
    from core.config import settings
    monkeypatch.setattr(settings, "AGENT_MAX_WAITING_DRAFTS", 2, raising=False)
    res = actions.start_drafts(db, s.user_id, [env.other.lead_ids[0], s.lead_ids[4], s.lead_ids[5], s.lead_ids[6],
                                               s.lead_ids[0], s.lead_ids[7], s.lead_ids[8]])
    codes = [(r.lead_id, r.code) for r in res.refused]
    assert codes == [(env.other.lead_ids[0], "not_found"), (s.lead_ids[4], "never_contact"),
                     (s.lead_ids[5], "skipped"), (s.lead_ids[6], "wrong_state"), (s.lead_ids[0], "wrong_state"),
                     (s.lead_ids[8], "draft_limit")]
    assert [r.lead.id for r in res.items] == [s.lead_ids[7]] and res.items[0].busy
    assert first.items[0].server_status == "drafting"
    assert store.usage_today(db, s.user_id).drafts == 2


def test_the_daily_draft_cap_is_a_refusal(env, monkeypatch):
    from core.config import settings
    monkeypatch.setattr(settings, "AGENT_DAILY_DRAFT_CAP", 1, raising=False)
    res = actions.start_drafts(env.db, env.seed.user_id, env.seed.lead_ids[2:4])
    assert len(res.items) == 1 and res.refused[0].code == "draft_limit"


def test_approve_records_the_exact_copy_and_never_sends(env):
    db, s = env.db, env.seed
    oid = _draft(env)
    before = (db.query(EmailSent).count(), db.query(CreditLedger).count(),
              db.query(UserCredit).filter_by(user_id=s.user_id).one().used_credits)
    with pytest.raises(store.VersionConflict):
        actions.approve(db, s.user_id, oid, expected_version=2)
    out = actions.approve(db, s.user_id, oid, expected_version=1)
    db.commit()
    assert out.sent is False and out.message == "Approved. Sending comes next, so nothing has gone out."
    assert (out.outreach.status_word, out.outreach.can_approve, out.outreach.send_blocker) == \
        ("scheduled", False, "sending_off")
    row = db.get(AgentDraft, int(oid[2:]))
    assert row.approved_version == 1 and row.approved_hash == store.approval_hash(row.subject, row.body)
    again = actions.approve(db, s.user_id, oid, expected_version=1)
    assert again.outreach.status_word == "scheduled"
    after = (db.query(EmailSent).count(), db.query(CreditLedger).count(),
             db.query(UserCredit).filter_by(user_id=s.user_id).one().used_credits)
    assert before == after


def test_edit_keeps_the_students_words_and_clears_the_approval(env):
    db, s = env.db, env.seed
    oid = _draft(env)
    actions.approve(db, s.user_id, oid, expected_version=1)
    out = actions.edit(db, s.user_id, oid, body="Hi Priya \u2014 my own words.", expected_version=1)
    db.commit()
    assert out.body == "Hi Priya, my own words." and out.version == 2 and out.server_status == "needs_review"
    assert out.approved_at is None and out.versions[0].version == 1
    assert db.get(AgentDraft, int(oid[2:])).edited_by == "student"
    with pytest.raises(ActionError) as e:
        actions.edit(db, s.user_id, f"e_{s.email_ids[0]}", body="x", expected_version=1)
    assert e.value.code == "wrong_state"


def test_revise_on_an_approved_draft_clears_the_approval(env, monkeypatch):
    db, s = env.db, env.seed
    oid = _draft(env)
    actions.approve(db, s.user_id, oid)
    res = actions.revise(db, s.user_id, oid, "Make it shorter.", expected_version=1)
    db.commit()
    assert res.previous.version == 1 and res.outreach.version == 2 and res.outreach.status_word == "needs_you"
    assert store.usage_today(db, s.user_id).revisions == 1
    from core.config import settings
    monkeypatch.setattr(settings, "AGENT_DAILY_REVISION_CAP", 1, raising=False)
    with pytest.raises(ActionError) as e:
        actions.revise(db, s.user_id, oid, "Shorter still.")
    assert (e.value.code, e.value.status) == ("draft_limit", 429)


def test_a_filtered_instruction_is_invalid(env, monkeypatch):
    db, s = env.db, env.seed
    oid = _draft(env)

    def blocked(*a, **k):
        raise drafting.ContentFilterError("blocked")
    monkeypatch.setattr(drafting, "generate_json", blocked)
    with pytest.raises(ActionError) as e:
        actions.revise(db, s.user_id, oid, "something the filter hates")
    assert (e.value.code, e.value.status) == ("invalid", 422)


def test_skip_and_restore(env):
    db, s = env.db, env.seed
    bare = actions.skip_lead(db, s.user_id, s.lead_ids[9], reason="Not my field")
    assert bare.status_word == "skipped" and bare.note == "You passed on this person."
    assert actions.skip_lead(db, s.user_id, s.lead_ids[9]).id == bare.id
    actions.restore(db, s.user_id, bare.id)
    assert db.get(AgentDraft, int(bare.id[2:])) is None
    oid = _draft(env, 4)
    actions.skip_lead(db, s.user_id, s.lead_ids[4])
    actions.restore(db, s.user_id, oid)
    assert db.get(AgentDraft, int(oid[2:])).status == "needs_review"
    with pytest.raises(ActionError):
        actions.skip_lead(db, s.user_id, s.lead_ids[0])   # already emailed


def test_discard_hides_the_draft_and_the_lead_is_a_match_again(env):
    db, s = env.db, env.seed
    oid = _draft(env)
    out = actions.discard(db, s.user_id, oid, reason="Wrong person")
    db.commit()
    assert out.server_status == "discarded"
    assert oid not in [r.id for r in reads.outreach_list(db, s.user_id).items]
    card = [c for c in reads.ranked_matches(db, s.user_id, limit=50).items if c.lead_id == s.lead_ids[3]][0]
    assert card.state == "new" and card.outreach_id is None


def test_approve_all_refuses_what_is_not_waiting(env):
    db, s = env.db, env.seed
    a, b = _draft(env, 3), _draft(env, 4)
    actions.approve(db, s.user_id, b)
    res = actions.approve_all(db, s.user_id, [a, b, f"e_{s.email_ids[0]}", "d_99999"])
    assert [r.id for r in res.approved] == [a] and res.sent is False
    assert sorted(r.code for r in res.refused) == ["not_found", "wrong_state", "wrong_state"]


def test_update_notes_merges_key_by_key_and_blank_removes(env):
    db, s = env.db, env.seed
    prof = actions.update_notes(db, s.user_id, UpdateNotes(why_now="Graduating in May"))
    assert prof.notes.why_now == "Graduating in May" and prof.notes.best_project.startswith("Built Splitkart")
    prof = actions.update_notes(db, s.user_id, {"best_project": ""})
    db.commit()
    assert prof.notes.best_project is None
    assert db.get(Candidate, s.candidate_id).flex_notes == {"why_now": "Graduating in May"}


def test_memories_cap_and_delete(env):
    db, s = env.db, env.seed
    m = actions.save_memory(db, s.user_id, "Keep it short", scope="emails", source="settings")
    assert m.source == "settings"
    assert actions.delete_memory(db, s.user_id, m.id).text == "Keep it short"
    with pytest.raises(ActionError) as e:
        actions.delete_memory(db, s.user_id, m.id)
    assert e.value.code == "not_found"
