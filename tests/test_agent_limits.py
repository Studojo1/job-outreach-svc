"""Student agent gates and caps (contract sections 0.6 and 2).

Pins: the AGENT_DISABLED kill switch answers 503 before any query; the flag
and the allowlist answer 403 (bootstrap answers 200 enabled false instead);
the daily message and token caps are 429 with no model call; one live run per
thread and two per user are 409; a repeated client_turn_id is 409 with the
first run id; a run whose heartbeat is stale is reaped as interrupted; a full
pod is 503 busy and gives its slot back.
"""

import json
import queue
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api import routes_agent
from core.config import settings
from database.models import AgentRun
from services.agent import gating, runtime, store, testing
from services.agent.schemas import SendMessage


@pytest.fixture
def env(monkeypatch):
    engine = testing.make_engine()
    factory = testing.use_engine(monkeypatch, engine)
    db = factory()
    seed = testing.seed_student(db)
    monkeypatch.setattr(settings, "AGENT_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_ALLOWLIST", seed.user_email.upper())
    monkeypatch.delenv("AGENT_DISABLED", raising=False)
    gating.reset_slots_for_tests()
    fake = testing.FakeLLM([]).install(monkeypatch)
    started = []
    monkeypatch.setattr(routes_agent, "_start_worker",
                        lambda run_id, q, slot, umid, conf: started.append((run_id, slot)))
    user = SimpleNamespace(id=seed.user_id, email=seed.user_email, name=seed.name, email_verified=True)
    thread = store.create_thread(db, seed.user_id)
    db.commit()
    yield SimpleNamespace(db=db, seed=seed, user=user, thread_id=thread.id, fake=fake, started=started)
    for _, slot in started:
        slot.release()
    db.close()
    gating.reset_slots_for_tests()


def _send(env, content="Hi", turn="turn-0001", thread_id=None):
    body = SendMessage(content=content, client_turn_id=turn)
    return routes_agent.send_message(thread_id or env.thread_id, body, current_user=env.user, db=env.db)


def _code(exc: HTTPException) -> str:
    return exc.detail["code"]


def test_the_kill_switch_is_a_503_before_any_query(monkeypatch, env):
    monkeypatch.setenv("AGENT_DISABLED", "1")
    with pytest.raises(HTTPException) as e:
        routes_agent.send_message(env.thread_id, SendMessage(content="Hi", client_turn_id="turn-0001"),
                                  current_user=env.user, db=None)
    assert e.value.status_code == 503 and _code(e.value) == "agent_disabled"
    out = routes_agent.bootstrap(current_user=env.user, db=None)
    assert json.loads(out.body) == {"enabled": False, "reason": "agent_disabled"}


def test_a_user_off_the_allowlist_gets_403_and_bootstrap_says_not_enabled(monkeypatch, env):
    monkeypatch.setattr(settings, "AGENT_ALLOWLIST", "someone.else@example.com")
    with pytest.raises(HTTPException) as e:
        routes_agent.get_status(current_user=env.user, db=None)
    assert e.value.status_code == 403 and _code(e.value) == "agent_not_enabled"
    out = routes_agent.bootstrap(current_user=env.user, db=None)
    assert json.loads(out.body) == {"enabled": False, "reason": "agent_not_enabled"}


def test_the_flag_off_or_an_empty_allowlist_lets_nobody_in(monkeypatch, env):
    monkeypatch.setattr(settings, "AGENT_ALLOWLIST", "")
    assert gating.gate_reason(env.user) == "agent_not_enabled"
    monkeypatch.setattr(settings, "AGENT_ALLOWLIST", "*")
    assert gating.gate_reason(env.user) is None
    monkeypatch.setattr(settings, "AGENT_ENABLED", False)
    assert gating.gate_reason(env.user) == "agent_not_enabled"


def test_an_unverified_email_never_matches_the_allowlist(monkeypatch, env):
    unverified = SimpleNamespace(id=env.seed.user_id, email=env.seed.user_email, name=env.seed.name,
                                 email_verified=False)
    assert gating.gate_reason(unverified) == "agent_not_enabled"
    assert gating.gate_reason(env.user) is None


def test_the_allowlist_matches_a_user_id_too(monkeypatch, env):
    monkeypatch.setattr(settings, "AGENT_ALLOWLIST", f"nobody@example.com, {env.seed.user_id}")
    assert gating.gate_reason(env.user) is None


def test_the_61st_message_of_the_day_is_a_429_with_no_model_call(env):
    store.bump_usage(env.db, env.seed.user_id, messages=settings.AGENT_DAILY_MESSAGE_CAP)
    env.db.commit()
    with pytest.raises(HTTPException) as e:
        _send(env)
    assert e.value.status_code == 429
    assert e.value.detail["code"] == "daily_limit" and e.value.detail["limit"] == "messages"
    assert e.value.detail["resets_at"]
    assert env.fake.calls == 0 and env.started == []


def test_the_daily_token_cap_is_a_429_too(env):
    store.bump_usage(env.db, env.seed.user_id, tokens=settings.AGENT_DAILY_TOKEN_CAP)
    env.db.commit()
    with pytest.raises(HTTPException) as e:
        _send(env)
    assert e.value.status_code == 429 and e.value.detail["limit"] == "tokens"


def test_a_second_message_on_a_thread_with_a_live_run_is_a_409(env):
    _send(env, turn="turn-0001")
    first_run = env.started[0][0]
    with pytest.raises(HTTPException) as e:
        _send(env, turn="turn-0002")
    assert e.value.status_code == 409
    assert e.value.detail["code"] == "run_in_progress" and e.value.detail["run_id"] == first_run


def test_a_third_live_run_for_the_user_is_a_409(env):
    t2 = store.create_thread(env.db, env.seed.user_id)
    t3 = store.create_thread(env.db, env.seed.user_id)
    env.db.commit()
    _send(env, turn="turn-0001")
    _send(env, turn="turn-0002", thread_id=t2.id)
    with pytest.raises(HTTPException) as e:
        _send(env, turn="turn-0003", thread_id=t3.id)
    assert e.value.detail["code"] == "run_in_progress"


def test_a_repeated_client_turn_id_is_a_409_with_the_first_run_id(env):
    _send(env, turn="turn-0001")
    first_run = env.started[0][0]
    env.db.query(AgentRun).filter(AgentRun.id == first_run).update({"status": "done"})
    env.db.commit()
    with pytest.raises(HTTPException) as e:
        _send(env, turn="turn-0001")
    assert e.value.detail["code"] == "duplicate_turn" and e.value.detail["run_id"] == first_run


def test_an_accepted_message_counts_toward_the_day(env):
    _send(env)
    assert store.usage_today(env.db, env.seed.user_id)["messages"] == 1


def test_a_run_with_a_two_minute_old_heartbeat_is_reaped_and_the_thread_frees_up(env):
    _send(env, turn="turn-0001")
    first_run = env.started[0][0]
    env.db.query(AgentRun).filter(AgentRun.id == first_run).update(
        {"heartbeat_at": datetime.utcnow() - timedelta(minutes=2)})
    env.db.commit()
    _send(env, turn="turn-0002")
    env.db.expire_all()
    run = env.db.get(AgentRun, first_run)
    assert run.status == "error" and run.error_code == "interrupted"
    assert run.events[-1]["type"] == "error" and run.events[-1]["code"] == "interrupted"


def test_a_full_pod_is_503_busy_and_releases_nothing_it_did_not_take(monkeypatch, env):
    monkeypatch.setattr(settings, "AGENT_MAX_CONCURRENT_RUNS", 1)
    gating.reset_slots_for_tests()
    held = gating.acquire_slot()
    try:
        with pytest.raises(HTTPException) as e:
            _send(env)
        assert e.value.status_code == 503 and _code(e.value) == "busy"
    finally:
        held.release()
    gating.acquire_slot().release()


def test_a_refused_start_gives_the_slot_back(monkeypatch, env):
    monkeypatch.setattr(settings, "AGENT_MAX_CONCURRENT_RUNS", 1)
    gating.reset_slots_for_tests()
    body = SendMessage(content="", client_turn_id="turn-0009",
                       confirmation={"id": "c_000000000000", "decision": "approve"})
    with pytest.raises(HTTPException) as e:
        routes_agent.send_message(env.thread_id, body, current_user=env.user, db=env.db)
    assert e.value.detail["code"] == "wrong_state"
    gating.acquire_slot().release()


def test_the_run_slot_is_released_when_the_turn_ends(monkeypatch, env):
    monkeypatch.setattr(settings, "AGENT_MAX_CONCURRENT_RUNS", 1)
    gating.reset_slots_for_tests()
    slot = gating.acquire_slot()
    run_id, _, _ = testing.start_turn(env.db, env.seed.user_id, "Hi", thread_id=env.thread_id)
    runtime.run_turn(run_id, queue.Queue(), slot)
    gating.acquire_slot().release()
