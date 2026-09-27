"""A paid user with credits and nothing running is always routed to Launch,
and the sweep nudges the ones who never come back.

Fixtures mirror the six production users found stuck on 2026-09-27:
  - payment never linked to an order, order at campaign_setup (x2)
  - order still 'created' although the payment was linked, bound to a
    candidate with no finished profile while a finished one exists
  - campaign_setup with everything ready, just never launched (x3)
"""
import pathlib
import sys
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import (
    Base, Campaign, Candidate, EmailAccount, LaunchNudge, Lead, OutreachOrder, PaymentOrder,
    SystemEvent, User, UserCredit,
)
from services import launch_nudge
from services.next_step import (
    CAMPAIGN_ACTIVE, CONNECT_GMAIL, LAUNCH_DRAFT, LAUNCH_READY, NEEDS_PROFILE, NOT_PAID,
    resolve_next_step,
)


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


NOW = datetime(2026, 9, 27, 12, 0, 0)
DONE = {"career_analysis": {"x": 1}}


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (
        User, Candidate, Lead, Campaign, EmailAccount, OutreachOrder, PaymentOrder,
        UserCredit, SystemEvent, LaunchNudge)])
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _user(db, uid, *, credits=200, used=0, paid_days_ago=1.0, linked=True,
          order_status="campaign_setup", gmail=True, candidates=((True, 800),),
          order_candidate=0):
    db.add(User(id=uid, email=f"{uid}@x.com", name=f"{uid.title()} Surname",
                email_verified=True, created_at=NOW, updated_at=NOW))
    cids = []
    for i, (finished, leads) in enumerate(candidates):
        cid = abs(hash((uid, i))) % 10_000_000
        db.add(Candidate(id=cid, user_id=uid, resume_text=".",
                         parsed_json=DONE if finished else {},
                         created_at=NOW - timedelta(days=10 - i)))
        for n in range(leads):
            db.add(Lead(candidate_id=cid, name=f"l{n}"))
        cids.append(cid)
    order = OutreachOrder(user_id=uid, status=order_status,
                          candidate_id=cids[order_candidate] if cids else None,
                          created_at=NOW - timedelta(days=10), action_log=[])
    db.add(order)
    db.flush()
    db.add(PaymentOrder(user_id=uid, status="paid", amount_cents=170000, currency="INR",
                        tier=credits, credits_granted=credits,
                        outreach_order_id=order.id if linked else None,
                        created_at=NOW - timedelta(days=paid_days_ago)))
    if credits:
        db.add(UserCredit(user_id=uid, total_credits=credits, used_credits=used))
    if gmail:
        db.add(EmailAccount(user_id=uid, email_address=f"{uid}@gmail.com",
                            provider="gmail", access_token="t", created_at=NOW))  # noqa: S106
    db.commit()
    return order, cids


# ── routing ─────────────────────────────────────────────────────────────────

def test_unlinked_payment_at_campaign_setup_goes_to_launch(db):
    _user(db, "raina", linked=False)
    step = resolve_next_step(db, "raina")
    assert (step.state, step.path) == (LAUNCH_READY, "/campaign/setup")
    assert step.email_account_id is not None


def test_order_stuck_at_created_is_promoted_and_sent_to_launch_on_the_finished_profile(db):
    # Sparsh: order bound to an unfinished candidate that has leads, a finished
    # one exists, order status never left 'created'.
    order, cids = _user(db, "sparsh", order_status="created",
                        candidates=((True, 8), (False, 16)), order_candidate=1)
    step = resolve_next_step(db, "sparsh")
    assert step.state == LAUNCH_READY
    assert step.candidate_id == cids[0]
    db.refresh(order)
    assert order.status == "campaign_setup"
    assert order.email_account_id == step.email_account_id


def test_profile_complete_is_no_longer_a_dead_end(db):
    order, _ = _user(db, "pc", order_status="profile_complete")
    assert resolve_next_step(db, "pc").state == LAUNCH_READY
    db.refresh(order)
    assert order.status == "campaign_setup"


def test_paid_without_gmail_goes_to_connect(db):
    _user(db, "nogmail", gmail=False)
    step = resolve_next_step(db, "nogmail")
    assert (step.state, step.path) == (CONNECT_GMAIL, "/connect/gmail")


def test_paid_without_a_finished_profile_goes_to_upload(db):
    _user(db, "noprof", candidates=((False, 5),))
    assert resolve_next_step(db, "noprof").state == NEEDS_PROFILE


def test_running_campaign_goes_to_dashboard(db):
    _, cids = _user(db, "running", used=200)
    db.add(Campaign(candidate_id=cids[0], name="c", status="running", created_at=NOW))
    db.commit()
    assert resolve_next_step(db, "running").state == CAMPAIGN_ACTIVE


def test_draft_is_launched_not_recreated(db):
    _, cids = _user(db, "drafty", used=200)
    db.add(Campaign(id=77, candidate_id=cids[0], name="c", status="draft", created_at=NOW))
    db.commit()
    step = resolve_next_step(db, "drafty")
    assert (step.state, step.campaign_id) == (LAUNCH_DRAFT, 77)


def test_unpaid_user_keeps_the_normal_funnel(db):
    _user(db, "free", credits=0)
    assert resolve_next_step(db, "free").state == NOT_PAID


def test_heal_false_changes_nothing(db):
    order, _ = _user(db, "ro", order_status="created")
    resolve_next_step(db, "ro", heal=False)
    db.refresh(order)
    assert order.status == "created"


# ── sweep ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(launch_nudge, "_send_template", lambda payload: calls.append(payload) or True)
    return calls


def _nudges(sent):
    return [c for c in sent if c["template"] == "outreach-launch-nudge"]


def test_recent_payer_is_nudged_with_a_link_to_launch(db, sent):
    _user(db, "likhitha", credits=500, paid_days_ago=3)
    result = launch_nudge.sweep(db, now=NOW)
    [nudge] = _nudges(sent)
    assert nudge["to"] == "likhitha@x.com"
    assert nudge["action_url"].endswith("/campaign/setup")
    assert nudge["credits"] == 500
    assert nudge["user_name"] == "Likhitha"
    assert [r["user_id"] for r in result["nudged"]] == ["likhitha"]
    assert any(c["template"] == "ops-alert" for c in sent)


def test_not_nudged_before_two_hours(db, sent):
    _user(db, "fresh", paid_days_ago=1 / 24)
    assert launch_nudge.sweep(db, now=NOW)["nudged"] == []


def test_old_payers_reach_the_founders_but_are_never_auto_emailed(db, sent):
    _user(db, "may", paid_days_ago=140)
    result = launch_nudge.sweep(db, now=NOW)
    assert _nudges(sent) == []
    assert [r["user_id"] for r in result["stuck"]] == ["may"]
    [alert] = [c for c in sent if c["template"] == "ops-alert"][:1]
    assert "may@x.com" in alert["message"]


def test_nudges_are_spaced_and_capped(db, sent):
    _user(db, "spaced", paid_days_ago=0.5)
    t = NOW
    launch_nudge.sweep(db, now=t)                           # nudge 1
    launch_nudge.sweep(db, now=t + timedelta(hours=5))      # too soon
    launch_nudge.sweep(db, now=t + timedelta(hours=25))     # nudge 2
    launch_nudge.sweep(db, now=t + timedelta(hours=60))     # too soon
    launch_nudge.sweep(db, now=t + timedelta(hours=98))     # nudge 3
    launch_nudge.sweep(db, now=t + timedelta(days=9))       # capped
    assert len(_nudges(sent)) == 3


def test_launched_payers_with_leftover_credits_are_left_alone(db, sent):
    _, cids = _user(db, "done", paid_days_ago=3)
    db.add(Campaign(candidate_id=cids[0], name="c", status="completed", created_at=NOW - timedelta(days=2)))
    db.commit()
    assert launch_nudge.sweep(db, now=NOW)["stuck"] == []
    assert sent == []


def test_failed_send_is_not_recorded_so_it_retries(db, monkeypatch):
    monkeypatch.setattr(launch_nudge, "_send_template", lambda payload: False)
    _user(db, "retry", paid_days_ago=1)
    assert launch_nudge.sweep(db, now=NOW)["nudged"] == []
    assert db.query(LaunchNudge).count() == 0
    assert db.query(SystemEvent).filter_by(event_type=launch_nudge.DIGEST_EVENT).count() == 0


def test_daily_digest_fires_once_a_day_without_nudges(db, sent):
    _user(db, "may", paid_days_ago=140)
    launch_nudge.sweep(db, now=NOW)
    launch_nudge.sweep(db, now=NOW + timedelta(hours=3))
    launch_nudge.sweep(db, now=NOW + timedelta(hours=25))
    assert len([c for c in sent if c["template"] == "ops-alert"]) == 2 * 2  # two founders, twice


def test_maybe_sweep_runs_at_most_hourly(db, sent, monkeypatch):
    runs = []
    monkeypatch.setattr(launch_nudge, "sweep", lambda db, now=None: runs.append(now) or {"stuck": [], "nudged": []})
    launch_nudge.maybe_sweep(db)
    launch_nudge.maybe_sweep(db)
    assert len(runs) == 1


def test_coupon_only_accounts_are_never_nudged(db, sent):
    # OAUTH100 is how Google's OAuth reviewers get in; they must not be mailed.
    _user(db, "reviewer", paid_days_ago=1)
    db.query(PaymentOrder).filter_by(user_id="reviewer").update({"amount_cents": 0, "provider": "coupon"})
    db.commit()
    assert launch_nudge.sweep(db, now=NOW)["stuck"] == []
    assert sent == []
