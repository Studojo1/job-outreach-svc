"""services/agent/store.py: the only reader and writer of the agent tables.

Pins ownership (user B never reaches user A's rows), the run lifecycle that
resume and cancel depend on (persisted events, cross-session cancel, the
stale-run reaper), duplicate turns, the draft record (versions, approval hash,
the one live row per lead, the 5 minute reaper), tickets, memories, settings
and the IST day counters behind the caps.
"""
from datetime import datetime, timedelta

import pytest

from database.models import AgentDraft, AgentMessage, AgentRun
from services.agent import store
from services.agent.testing import make_engine, seed_student, session_factory, start_turn


@pytest.fixture()
def db():
    s = session_factory(make_engine())()
    s.a = seed_student(s, user_id="ua")
    s.b = seed_student(s, user_id="ub")
    yield s
    s.close()


def _run_start(run_id, thread_id, seq=1):
    return {"type": "run_start", "seq": seq, "run_id": run_id, "thread_id": thread_id, "user_message_id": 1}


# ---------------------------------------------------------------- ownership

def test_user_b_cannot_reach_any_of_user_a_rows(db):
    a = db.a
    run_id, _, thread_id = start_turn(db, a.user_id, "Hello")
    draft = store.insert_drafting(db, a.user_id, a.candidate_id, a.lead_ids[0])
    ticket = store.create_confirmation(db, a.user_id, thread_id, "update_profile_notes", {"outcome": "x"}, "Save it")
    memory = store.save_memory(db, a.user_id, "Keep emails short", scope="emails")
    db.commit()

    for call in (
        lambda: store.get_thread(db, thread_id, "ub"),
        lambda: store.display_messages(db, thread_id, "ub"),
        lambda: store.history_for_model(db, thread_id, "ub"),
        lambda: store.get_run(db, run_id, "ub"),
        lambda: store.events_after(db, run_id, "ub"),
        lambda: store.request_cancel(db, run_id, "ub"),
        lambda: store.append_event(db, run_id, _run_start(run_id, thread_id), user_id="ub"),
        lambda: store.get_draft(db, draft.id, "ub"),
        lambda: store.update_draft_copy(db, draft.id, "ub", subject="x", body="y"),
        lambda: store.set_draft_status(db, draft.id, "ub", "discarded"),
        lambda: store.delete_draft(db, draft.id, "ub"),
        lambda: store.get_confirmation(db, ticket.id, "ub"),
        lambda: store.decide_confirmation(db, ticket.id, "ub", "approved"),
        lambda: store.delete_memory(db, memory.id, "ub"),
        lambda: store.archive_thread(db, thread_id, "ub"),
        lambda: store.create_confirmation(db, "ub", thread_id, "update_profile_notes", {}, "x"),
        lambda: store.add_user_message(db, thread_id, "ub", "hi"),
        lambda: store.start_run(db, thread_id, "ub", _body()),
    ):
        with pytest.raises(store.NotFound):
            call()
    assert store.list_threads(db, "ub") == []
    assert store.list_drafts(db, "ub") == []
    assert store.list_memories(db, "ub") == []
    assert store.live_runs(db, "ub") == []


def test_drafting_another_users_lead_or_candidate_is_not_found(db):
    with pytest.raises(store.NotFound):
        store.insert_drafting(db, "ub", db.a.candidate_id, db.a.lead_ids[0])
    with pytest.raises(store.NotFound):
        store.insert_drafting(db, "ua", db.a.candidate_id, db.b.lead_ids[0])


def _body(content="Hi", turn="turn-0001", **kw):
    from services.agent.schemas import SendMessage
    return SendMessage(content=content, client_turn_id=turn, **kw)


# ---------------------------------------------------------------- runs

def test_start_run_commits_message_and_run_and_titles_the_thread(db):
    run_id, msg_id, thread_id = start_turn(db, "ua", "Write to my top five matches please",
                                           page_context={"route": "home", "filter": "needs_you"})
    db.rollback()  # nothing pending: start_run committed
    run = store.get_run(db, run_id, "ua")
    msg = db.get(AgentMessage, msg_id)
    assert run.status == "running" and run.events == [] and msg.run_id == run_id
    assert msg.meta["page_context"]["route"] == "home"
    assert store.get_thread(db, thread_id, "ua").title == "Write to my top five matches please"


def test_repeated_client_turn_id_raises_duplicate_turn_with_the_first_run(db):
    run_id, _, thread_id = start_turn(db, "ua", "Hi", client_turn_id="same-turn-1")
    with pytest.raises(store.DuplicateTurn) as exc:
        store.start_run(db, thread_id, "ua", _body("Hi again", "same-turn-1"))
    assert exc.value.run_id == run_id
    assert db.query(AgentRun).filter(AgentRun.thread_id == thread_id).count() == 1


def test_confirmation_turn_with_no_text_is_stored_as_confirm(db):
    run_id, msg_id, _ = start_turn(db, "ua", "", confirmation={"id": "c_0123456789ab", "decision": "approve"})
    assert db.get(AgentMessage, msg_id).content == "Confirm"
    assert store.get_run(db, run_id, "ua").confirmation_id == "c_0123456789ab"


def test_events_persist_in_order_and_replay_after_a_seq(db):
    run_id, _, thread_id = start_turn(db, "ua")
    store.append_event(db, run_id, _run_start(run_id, thread_id))
    store.append_event(db, run_id, {"type": "tool_start", "seq": 3, "call_id": "c1", "tool": "get_status",
                                    "label": "Checking where things stand"})
    other = session_factory(db.get_bind())()  # another pod
    assert [e["seq"] for e in store.events_after(other, run_id, "ua", 0)] == [1, 3]
    assert [e["type"] for e in store.events_after(other, run_id, "ua", 1)] == ["tool_start"]
    assert store.events_after(other, run_id, "ua", 3) == []


def test_cancel_is_seen_from_another_session_and_is_idempotent(db):
    run_id, _, _ = start_turn(db, "ua")
    worker = session_factory(db.get_bind())()
    assert store.cancel_requested(worker, run_id) is False
    store.request_cancel(db, run_id, "ua")
    first = store.get_run(db, run_id, "ua").cancel_requested_at
    store.request_cancel(db, run_id, "ua")
    assert store.get_run(db, run_id, "ua").cancel_requested_at == first
    assert store.cancel_requested(worker, run_id) is True


def test_finish_moves_a_running_run_once_and_keeps_the_final_message(db):
    run_id, _, thread_id = start_turn(db, "ua")
    store.save_final_message(db, run_id, "Done here.", meta={"status": "done", "outreach_ids": ["d_1"]})
    assert store.finish_run(db, run_id, "done") is True
    assert store.finish_run(db, run_id, "cancelled") is False
    assert store.get_run(db, run_id, "ua").status == "done"
    msg = store.final_message(db, run_id, "ua")
    assert msg.content == "Done here." and msg.status == "done" and msg.outreach_ids == ["d_1"]


def test_failed_run_has_no_assistant_message(db):
    run_id, _, thread_id = start_turn(db, "ua")
    assert store.fail_run(db, run_id, "internal", "KeyError: secret detail") is True
    run = store.get_run(db, run_id, "ua")
    assert (run.status, run.error_code) == ("error", "internal")
    assert store.run_out(run).error_code == "internal"
    msgs, _ = store.display_messages(db, thread_id, "ua")
    assert [m.role for m in msgs] == ["user"]
    assert store.final_message(db, run_id, "ua") is None


def test_stale_run_is_reaped_to_interrupted_with_a_persisted_error_frame(db):
    run_id, _, thread_id = start_turn(db, "ua")
    store.append_event(db, run_id, _run_start(run_id, thread_id))
    fresh, _, _ = start_turn(db, "ua")  # a live run on another thread
    db.query(AgentRun).filter(AgentRun.id == run_id).update(
        {"heartbeat_at": datetime.utcnow() - timedelta(minutes=2)})
    db.commit()
    assert store.reap_stale_runs(db, thread_id) == [run_id]
    run = store.get_run(db, run_id, "ua")
    assert (run.status, run.error_code) == ("error", "interrupted")
    last = store.events_after(db, run_id, "ua", 1)
    assert last == [{"type": "error", "seq": 2, "code": "interrupted", "retryable": True,
                     "message": "That turn was interrupted. Please send it again."}]
    assert store.get_run(db, fresh, "ua").status == "running"
    assert [r.id for r in store.live_runs(db, "ua")] == [fresh]


def test_usage_lands_on_the_run_and_on_today(db):
    run_id, _, _ = start_turn(db, "ua")
    store.add_usage(db, run_id, {"prompt_tokens": 100, "completion_tokens": 20}, steps=1)
    store.add_usage(db, run_id, tool_calls=2, drafts_created=1)
    run = store.get_run(db, run_id, "ua")
    assert (run.tokens_in, run.tokens_out, run.steps, run.tool_calls, run.drafts_created) == (100, 20, 1, 2, 1)
    assert store.usage_today(db, "ua").tokens == 120
    assert store.usage_today(db, "ub").tokens == 0


# ---------------------------------------------------------------- messages and history

def test_display_pages_bubbles_and_hides_tool_rows(db):
    run_id, _, thread_id = start_turn(db, "ua", "First")
    store.save_assistant_tool_calls(db, run_id, [{"id": "c1", "type": "function",
                                                  "function": {"name": "get_status", "arguments": "{}"}}])
    store.save_tool_result(db, run_id, "c1", "get_status", "x" * 9000, ok=True, summary="Checked")
    store.save_final_message(db, run_id, "Answer", meta={"status": "done", "cards": [{"kind": "nonsense"}],
                                                          "suggestions": [{"label": "A", "prompt": "B"}]})
    store.finish_run(db, run_id, "done")
    msgs, more = store.display_messages(db, thread_id, "ua")
    assert [(m.role, m.content) for m in msgs] == [("user", "First"), ("assistant", "Answer")]
    assert msgs[1].cards == [] and msgs[1].suggestions[0].label == "A" and not more
    tool_row = db.query(AgentMessage).filter(AgentMessage.role == "tool").one()
    assert len(tool_row.content) == 6000
    page, more = store.display_messages(db, thread_id, "ua", limit=1)
    assert [m.content for m in page] == ["Answer"] and more
    older, more = store.display_messages(db, thread_id, "ua", before=page[0].id, limit=1)
    assert [m.content for m in older] == ["First"] and not more


def test_history_keeps_tool_pairs_whole_and_summarises_old_tool_rows(db):
    thread_id = None
    for i in range(5):
        run_id, _, thread_id = start_turn(db, "ua", f"Q{i}", thread_id=thread_id)
        store.save_assistant_tool_calls(db, run_id, [{"id": f"c{i}", "type": "function",
                                                      "function": {"name": "get_status", "arguments": "{}"}}])
        store.save_tool_result(db, run_id, f"c{i}", "get_status", f"FULL{i}", summary=f"sum{i}")
        store.save_final_message(db, run_id, f"A{i}")
        store.finish_run(db, run_id, "done")
    # A run that died after asking for a tool: its unanswered call must not reach the model.
    dead, _, _ = start_turn(db, "ua", "Q5", thread_id=thread_id)
    store.save_assistant_tool_calls(db, dead, [{"id": "c5", "type": "function",
                                                "function": {"name": "get_status", "arguments": "{}"}}])
    store.fail_run(db, dead, "internal")
    hist = store.history_for_model(db, thread_id, "ua")
    tools = [m["content"] for m in hist if m["role"] == "tool"]
    assert tools == ["sum0", "sum1", "sum2", "FULL3", "FULL4"]
    assert hist[-1] == {"role": "user", "content": "Q5"}
    assert not any(m.get("tool_calls") and m["tool_calls"][0]["id"] == "c5" for m in hist)
    cut = store.history_for_model(db, thread_id, "ua", max_chars=30)
    assert cut == [{"role": "user", "content": "Q5"}]


# ---------------------------------------------------------------- drafts

def _filled(db, lead_index=0, user="ua"):
    seed = db.a if user == "ua" else db.b
    d = store.insert_drafting(db, user, seed.candidate_id, seed.lead_ids[lead_index], style="warm_intro")
    store.update_draft_copy(db, d.id, user, subject="quick question", body="Hi Ananya",
                            facts_used=[{"text": "Built Splitkart", "source": "profile"}])
    db.commit()
    return d


def test_first_fill_is_version_one_and_keeps_the_original(db):
    d = _filled(db)
    assert (d.status, d.version, d.versions) == ("needs_review", 1, [])
    assert (d.original_subject, d.original_body, d.heartbeat_at) == ("quick question", "Hi Ananya", None)


def test_revise_pushes_the_old_copy_and_clears_approval(db):
    d = _filled(db)
    store.set_draft_status(db, d.id, "ua", "approved", expected_version=1)
    assert d.approved_version == 1 and d.approved_hash == store.approval_hash("quick question", "Hi Ananya")
    store.update_draft_copy(db, d.id, "ua", subject="shorter", body="Hi", edited_by="student", expected_version=1)
    assert (d.status, d.version, d.edited_by) == ("needs_review", 2, "student")
    assert (d.approved_at, d.approved_version, d.approved_hash) == (None, None, None)
    assert [v["version"] for v in d.versions] == [1] and d.versions[0]["subject"] == "quick question"
    store.update_draft_copy(db, d.id, "ua", subject="v3", body="b3", expected_version=2)
    assert [v["version"] for v in d.versions] == [2, 1]


def test_stale_version_raises_version_conflict_with_the_current_row(db):
    d = _filled(db)
    with pytest.raises(store.VersionConflict) as exc:
        store.update_draft_copy(db, d.id, "ua", subject="x", body="y", expected_version=3)
    assert exc.value.current.id == d.id and exc.value.current.version == 1
    with pytest.raises(store.VersionConflict):
        store.set_draft_status(db, d.id, "ua", "approved", expected_version=2)


def test_copy_cannot_change_on_a_skipped_or_discarded_row(db):
    d = _filled(db)
    store.set_draft_status(db, d.id, "ua", "discarded", reason="not now")
    with pytest.raises(store.WrongState):
        store.update_draft_copy(db, d.id, "ua", subject="x", body="y")


def test_one_live_row_per_lead_and_a_failed_row_is_superseded(db):
    d = store.insert_drafting(db, "ua", db.a.candidate_id, db.a.lead_ids[0])
    with pytest.raises(store.WrongState) as exc:
        store.insert_drafting(db, "ua", db.a.candidate_id, db.a.lead_ids[0])
    assert exc.value.reason == "wrong_state" and exc.value.row.id == d.id
    store.set_draft_status(db, d.id, "ua", "failed", reason="writer error")
    again = store.insert_drafting(db, "ua", db.a.candidate_id, db.a.lead_ids[0])
    db.commit()
    assert db.get(AgentDraft, d.id).status == "superseded" and again.status == "drafting"


def test_skipping_a_bare_match_blocks_drafting_with_reason_skipped(db):
    row = store.insert_skipped(db, "ua", db.a.candidate_id, db.a.lead_ids[3], reason="not my field")
    assert (row.status, row.subject, row.body, row.skip_reason) == ("skipped", "", "", "not my field")
    with pytest.raises(store.WrongState) as exc:
        store.insert_drafting(db, "ua", db.a.candidate_id, db.a.lead_ids[3])
    assert exc.value.reason == "skipped"
    assert store.live_rows_by_lead(db, "ua", db.a.candidate_id)[db.a.lead_ids[3]].id == row.id
    store.delete_draft(db, row.id, "ua")
    assert store.live_rows_by_lead(db, "ua", db.a.candidate_id) == {}


def test_drafting_row_silent_for_five_minutes_reads_as_failed(db):
    d = store.insert_drafting(db, "ua", db.a.candidate_id, db.a.lead_ids[1])
    live = store.insert_drafting(db, "ua", db.a.candidate_id, db.a.lead_ids[2])
    d.heartbeat_at = datetime.utcnow() - timedelta(minutes=6)
    db.commit()
    assert store.waiting_count(db, "ua") == 1
    assert store.get_draft(db, d.id, "ua").status == "failed"
    assert store.get_draft(db, live.id, "ua").status == "drafting"
    assert [x.id for x in store.list_drafts(db, "ua", statuses=["failed"])] == [d.id]


def test_restore_to_needs_review_clears_the_skip(db):
    d = _filled(db)
    store.set_draft_status(db, d.id, "ua", "skipped", reason="later")
    store.set_draft_status(db, d.id, "ua", "needs_review")
    assert (d.status, d.skipped_at, d.skip_reason) == ("needs_review", None, None)


# ---------------------------------------------------------------- tickets, memories, settings

def test_ticket_is_decided_once_and_expires(db):
    _, _, thread_id = start_turn(db, "ua")
    c = store.create_confirmation(db, "ua", thread_id, "update_profile_notes", {"outcome": "Won a hackathon"},
                                  "Save your outcome.")
    assert c.id.startswith("c_") and len(c.id) == 14
    assert store.get_confirmation(db, c.id, "ua", thread_id=thread_id, require_pending=True).args == \
        {"outcome": "Won a hackathon"}
    with pytest.raises(store.NotFound):
        store.get_confirmation(db, c.id, "ua", thread_id=thread_id + 99)
    store.decide_confirmation(db, c.id, "ua", "approved", result={"ok": True})
    with pytest.raises(store.WrongState):
        store.decide_confirmation(db, c.id, "ua", "approved")
    old = store.create_confirmation(db, "ua", thread_id, "update_profile_notes", {}, "Old", ttl_minutes=-1)
    assert store.confirmation_out(old).status == "expired"
    with pytest.raises(store.WrongState):
        store.get_confirmation(db, old.id, "ua", require_pending=True)


def test_memories_dedupe_cap_at_thirty_and_hard_delete(db):
    first = store.save_memory(db, "ua", "Keep emails  short", scope="emails", use_in_emails=False)
    assert store.save_memory(db, "ua", "keep emails short", scope="emails").id == first.id
    for i in range(29):
        store.save_memory(db, "ua", f"Note {i}")
    with pytest.raises(store.WrongState):
        store.save_memory(db, "ua", "One too many")
    gone = store.delete_memory(db, first.id, "ua")
    assert gone.text == "Keep emails short" and len(store.list_memories(db, "ua")) == 29
    assert store.save_memory(db, "ub", "Mine").user_id == "ub"


def test_settings_default_then_upsert_and_dedupe_never_contact(db):
    assert store.get_settings(db, "ua").model_dump() == {"review_before_send": True, "followups": True,
                                                         "never_contact": []}
    out = store.update_settings(db, "ua", followups=False,
                                never_contact=[{"kind": "company", "value": "Company3"},
                                               {"kind": "company", "value": " company3 "}])
    assert out.followups is False and out.review_before_send is True and len(out.never_contact) == 1
    assert store.get_settings(db, "ub").followups is True


def test_usage_counts_per_ist_day(db):
    late_utc = datetime(2026, 10, 12, 18, 29)   # 23:59 IST on the 12th
    after = datetime(2026, 10, 12, 18, 31)      # 00:01 IST on the 13th
    store.bump_usage(db, "ua", messages=1, now=late_utc)
    store.bump_usage(db, "ua", messages=1, drafts=2, now=late_utc)
    assert store.usage_today(db, "ua", now=late_utc) == {"messages": 2, "drafts": 2, "revisions": 0, "tokens": 0}
    assert store.usage_today(db, "ua", now=after).messages == 0
    assert store.resets_at(late_utc) == "2026-10-12T18:30:00"
