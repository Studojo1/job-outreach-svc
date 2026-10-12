"""Pins the tool layer (contract section 6): the registry, argument checks,
the result prefixes, ownership through candidates.user_id, the frames each
tool emits, the invalidate keys, the match ranking parity with
GET /candidate/{id}/leads, and that no lead address reaches the model."""

import json
from types import SimpleNamespace

import pytest

from database.models import AgentConfirmation, Candidate
from services.agent import context, events, store, tools
from services.agent.testing import (
    inline_drafting, make_engine, patch_writer, seed_student, session_factory, use_engine,
)

NAMES = {"get_profile", "get_status", "list_matches", "get_match", "list_outreach", "get_outreach", "draft_emails",
         "revise_draft", "skip", "remember", "forget", "navigate", "update_profile_notes"}


class Frames:
    """Records frames; each one must validate as a StreamEvent, as the real Emitter requires."""

    def __init__(self):
        self.frames = []

    def emit(self, event):
        events.validate({**event, "seq": len(self.frames) + 1})
        self.frames.append(event)
        return len(self.frames)


@pytest.fixture()
def env(monkeypatch):
    engine = make_engine()
    use_engine(monkeypatch, engine)
    db = session_factory(engine)()
    seed = seed_student(db)
    other = seed_student(db, user_id="other", name="Other Person", matches=4, with_email=1)
    writes = patch_writer(monkeypatch)
    inline_drafting(monkeypatch)
    thread = store.create_thread(db, seed.user_id)
    db.commit()
    ctx = context.ToolContext(user_id=seed.user_id, first_name="Asha", user_name="Asha Verma",
                              candidate_id=seed.candidate_id, thread_id=thread.id, run_id=None)
    yield SimpleNamespace(db=db, seed=seed, other=other, ctx=ctx, writes=writes, out=Frames())
    db.close()


def call(env, name, args=None, raw=None):
    return tools.dispatch(env.ctx, {"id": f"c_{name}", "name": name,
                                    "arguments": raw if raw is not None else json.dumps(args or {})}, env.out)


def test_the_registry_is_exactly_the_thirteen_contract_tools():
    assert set(tools.REGISTRY) == NAMES == set(tools.TOOL_SCHEMAS)
    assert [t["function"]["name"] for t in tools.TOOLS] == list(tools.TOOL_SCHEMAS)
    for t in tools.TOOLS:
        assert t["function"]["parameters"]["additionalProperties"] is False and t["function"]["description"]


def test_unknown_tools_bad_json_and_bad_arguments_are_answered_in_words(env):
    assert call(env, "send_email").for_model == "REFUSED: no such tool."
    assert call(env, "get_match", raw="{not json").for_model == "TOOL ERROR: invalid arguments: lead_id"
    assert call(env, "get_match", {"lead_id": "x"}).for_model == "TOOL ERROR: invalid arguments: lead_id"
    assert call(env, "list_matches", {"bogus": 1}).for_model == "TOOL ERROR: invalid arguments: bogus"
    assert env.out.frames == []


def test_a_student_without_a_resume_gets_no_profile(env):
    env.ctx.candidate_id = None
    res = call(env, "list_matches")
    assert res.ok is False and res.for_model.startswith("NO_PROFILE:")
    assert [f["type"] for f in env.out.frames] == ["tool_start", "tool_end"]


def test_the_tool_budget_refuses_past_the_cap(env, monkeypatch):
    from core.config import settings
    monkeypatch.setattr(settings, "AGENT_MAX_TOOL_CALLS", 2, raising=False)
    assert call(env, "get_status").ok and call(env, "get_status").ok
    assert call(env, "get_status").for_model.startswith("REFUSED: tool budget")


def test_another_students_lead_draft_email_and_memory_are_not_found(env):
    db, o = env.db, env.other
    other_mem = store.save_memory(db, o.user_id, "theirs")
    db.commit()
    assert call(env, "get_match", {"lead_id": o.lead_ids[0]}).for_model.startswith("NOT_FOUND:")
    assert call(env, "get_outreach", {"outreach_id": f"e_{o.email_ids[0]}"}).for_model.startswith("NOT_FOUND:")
    assert call(env, "skip", {"lead_id": o.lead_ids[1]}).for_model.startswith("NOT_FOUND:")
    assert call(env, "forget", {"memory_id": other_mem.id}).for_model.startswith("NOT_FOUND:")
    res = json.loads(call(env, "draft_emails", {"lead_ids": [o.lead_ids[1]]}).for_model)
    assert res["refused"][0]["code"] == "not_found" and res["drafted"] == []
    assert call(env, "navigate", {"to": "outreach", "id": f"e_{o.email_ids[0]}"}).for_model.startswith("NOT_FOUND:")


def test_list_matches_keeps_the_order_of_the_leads_route(env):
    from api.routes_candidate import get_candidate_leads
    resp = get_candidate_leads(SimpleNamespace(headers={}), env.seed.candidate_id, None, 0, None,
                               current_user=SimpleNamespace(id=env.seed.user_id), db=env.db)
    route_ids = [lead["id"] for lead in json.loads(resp.body)["leads"]]
    res = json.loads(call(env, "list_matches", {"limit": 25, "exclude_contacted": False}).for_model)
    assert [i["lead_id"] for i in res["items"]] == route_ids
    contacted = {i["lead_id"]: i for i in res["items"]}[env.seed.lead_ids[0]]
    assert contacted["state"] == "contacted" and contacted["outreach_id"] == f"e_{env.seed.email_ids[0]}"
    default = json.loads(call(env, "list_matches").for_model)
    assert env.seed.lead_ids[0] not in [i["lead_id"] for i in default["items"]]
    assert env.out.frames[-2]["type"] == "card" and env.out.frames[-2]["card"]["kind"] == "matches"


def test_get_match_without_a_justification_builds_bullets_from_the_scores(env):
    res = json.loads(call(env, "get_match", {"lead_id": env.seed.lead_ids[8]}).for_model)
    assert res["why"]["source"] == "score" and res["why"]["bullets"]
    assert "linkedin_url" not in res and "company_domain" not in res
    card = [f for f in env.out.frames if f["type"] == "card"][0]["card"]
    assert card["kind"] == "match" and card["match"]["lead_id"] == env.seed.lead_ids[8]


def test_drafting_emits_one_draft_frame_per_row_and_invalidates(env):
    ids = env.seed.lead_ids[2:5]
    res = call(env, "draft_emails", {"lead_ids": ids})
    body = json.loads(res.for_model)
    assert len(body["drafted"]) == 3 and body["still_writing"] == [] and res.invalidate == ["outreach", "matches",
                                                                                            "status"]
    types = [f["type"] for f in env.out.frames]
    assert types == ["tool_start", "draft", "draft", "draft", "tool_end"]
    assert env.out.frames[0]["label"] == "Writing 3 emails"
    assert all(f["outreach"]["status_word"] == "needs_you" for f in env.out.frames if f["type"] == "draft")


def test_drafts_per_run_are_capped(env, monkeypatch):
    from core.config import settings
    monkeypatch.setattr(settings, "AGENT_DRAFTS_PER_CALL", 2, raising=False)
    first = json.loads(call(env, "draft_emails", {"lead_ids": env.seed.lead_ids[2:5]}).for_model)
    assert len(first["drafted"]) == 2 and [r["code"] for r in first["refused"]] == ["draft_limit"]
    again = json.loads(call(env, "draft_emails", {"lead_ids": [env.seed.lead_ids[6]]}).for_model)
    assert again["drafted"] == [] and again["refused"][0]["code"] == "draft_limit"


def test_revise_emits_the_previous_copy_for_the_diff(env):
    call(env, "draft_emails", {"lead_ids": [env.seed.lead_ids[3]]})
    did = [f for f in env.out.frames if f["type"] == "draft"][0]["outreach"]["id"]
    res = call(env, "revise_draft", {"outreach_id": did, "instruction": "Make it shorter."})
    assert json.loads(res.for_model)["version"] == 2 and res.invalidate == ["outreach"]
    frame = [f for f in env.out.frames if f["type"] == "draft"][-1]
    assert frame["previous"]["version"] == 1 and frame["outreach"]["version"] == 2


def test_navigate_checks_the_route_the_id_and_the_filter(env):
    assert call(env, "navigate", {"to": "home", "filter": "nonsense"}).for_model == \
        "TOOL ERROR: invalid arguments: filter"
    assert call(env, "navigate", {"to": "home", "filter": "needs_you"}).ok
    nav = [f for f in env.out.frames if f["type"] == "navigate"]
    assert nav == [{"type": "navigate", "to": "home", "id": None, "filter": "needs_you"}]


def test_update_profile_notes_only_prepares_a_ticket(env):
    res = call(env, "update_profile_notes", {"best_project": "Built Splitkart, used by 1,300 students"})
    body = json.loads(res.for_model)
    assert body["status"] == "needs_confirmation" and body["confirmation_id"].startswith("c_")
    assert res.invalidate == []
    ticket = env.db.get(AgentConfirmation, body["confirmation_id"])
    assert ticket.status == "pending" and ticket.args == {"best_project": "Built Splitkart, used by 1,300 students"}
    env.db.expire_all()
    flex = env.db.get(Candidate, env.seed.candidate_id).flex_notes
    assert flex["best_project"] == "Built Splitkart, used by 1,200 students"
    assert [f["type"] for f in env.out.frames] == ["tool_start", "confirmation", "tool_end"]


def test_remember_and_forget_show_memory_cards(env):
    saved = json.loads(call(env, "remember", {"text": "Keep emails short", "scope": "emails"}).for_model)
    removed = json.loads(call(env, "forget", {"memory_id": saved["memory_id"]}).for_model)
    assert removed == {"removed": True, "text": "Keep emails short"}
    actions = [f["card"]["action"] for f in env.out.frames if f["type"] == "card"]
    assert actions == ["saved", "removed"]


def test_no_tool_result_or_frame_carries_an_address(env):
    s = env.seed
    call(env, "draft_emails", {"lead_ids": [s.lead_ids[2]]})
    for name, args in [("get_profile", {}), ("get_status", {}), ("list_matches", {"limit": 25}),
                       ("get_match", {"lead_id": s.lead_ids[0]}), ("get_match", {"lead_id": s.lead_ids[1]}),
                       ("list_outreach", {}), ("get_outreach", {"outreach_id": f"e_{s.email_ids[0]}"}),
                       ("skip", {"lead_id": s.lead_ids[7]})]:
        res = call(env, name, args)
        assert res.ok, (name, res.for_model)
        for secret in [*s.addresses, s.gmail_address, s.user_email]:
            assert secret not in res.for_model
    blob = json.dumps(env.out.frames)
    for secret in [*s.addresses, s.gmail_address, s.user_email]:
        assert secret not in blob
