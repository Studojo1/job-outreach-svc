"""Post-payment audit wave 2: a failed send no longer silently burns a paid slot.

P06 a dead Gmail grant pauses the campaign and keeps its queue (it used to
    fail all of it: campaign 100 lost 444 of 503 in 66 seconds);
    a transient Google error is retried; reconnecting resumes.
P13 a permanently failed paid first touch returns its credit, unless a
    replacement lead took the slot (P33) or it was a bounce replacement.
P01/P04/P10/P47 finishing a campaign returns credits for unsent work,
    retires that work, and closes the order.
P02 resuming re-plans from now, once (it also shifted by the pause length).
P24 cancel settles every unsent paid slot.
"""
import pathlib
import sys
from datetime import datetime, timedelta

import pytest
import requests
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import (
    Base, Campaign, Candidate, CreditLedger, EmailAccount, EmailSent, Lead, LeadScore,
    OutreachOrder, SuppressedEmail, User, UserCredit,
)
from services import credits
from services.email_campaign import campaign_worker, outcomes
from services.email_campaign.gmail_send_service import (
    GmailAuthError, GmailSendError, GmailTransientError,
)


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


NOW = datetime(2026, 9, 28, 12, 0, 0)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (
        User, Candidate, Lead, LeadScore, Campaign, EmailAccount, EmailSent, OutreachOrder,
        UserCredit, CreditLedger, SuppressedEmail)])
    session = sessionmaker(bind=engine)()
    session.add_all([
        User(id="u", email="u@x.com", name="U", email_verified=True, created_at=NOW, updated_at=NOW),
        Candidate(id=1, user_id="u", resume_text="."),
        EmailAccount(id=5, user_id="u", email_address="u@gmail.com", provider="gmail",
                     access_token="t", refresh_token="r", token_expiry=NOW + timedelta(days=9)),  # noqa: S106
        UserCredit(user_id="u", total_credits=10, used_credits=10),
        Campaign(id=10, candidate_id=1, email_account_id=5, name="c", status="running",
                 credits_reserved=10, credits_released=0),
        OutreachOrder(id=100, user_id="u", campaign_id=10, status="campaign_running", action_log=[]),
    ])
    session.commit()
    yield session
    session.close()


def _email(db, **kw):
    row = EmailSent(campaign_id=10, to_email="lead@x.com", subject=kw.pop("subject", "s"),
                    body=kw.pop("body", "b"), status=kw.pop("status", "queued"),
                    scheduled_at=kw.pop("scheduled_at", datetime.utcnow() - timedelta(minutes=1)),
                    enrichment_status="enriched", **kw)
    db.add(row)
    db.commit()
    return row


def _used(db):
    return db.query(UserCredit).filter_by(user_id="u").one().used_credits


# ── classification ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("exc,phase,expected", [
    (GmailAuthError("revoked"), "refresh", outcomes.PAUSE),
    (GmailAuthError("401"), "send", outcomes.PAUSE),
    (GmailTransientError("503"), "send", outcomes.RETRY),
    (GmailTransientError("will retry next cycle"), "refresh", outcomes.RETRY),
    (requests.exceptions.ConnectionError(), "send", outcomes.RETRY),
    (requests.exceptions.ReadTimeout(), "refresh", outcomes.RETRY),
    # A send that timed out may have been delivered: never resend it.
    (requests.exceptions.ReadTimeout(), "send", outcomes.FAIL),
    (GmailSendError("400 invalid to", 400), "send", outcomes.FAIL),
    (ValueError("anything else"), "send", outcomes.FAIL),
])
def test_classification(exc, phase, expected):
    assert outcomes.classify(exc, phase=phase) == expected


# ── the worker's send path ──────────────────────────────────────────────────

def _run_send(db, monkeypatch, exc):
    def boom(**kw):
        raise exc
    monkeypatch.setattr(campaign_worker, "send_gmail_email", boom)
    monkeypatch.setattr(campaign_worker, "_ensure_tracking_token", lambda e: None)
    monkeypatch.setattr(campaign_worker, "ph_capture", lambda *a, **k: None)
    return campaign_worker._send_ready(db)


def test_dead_gmail_pauses_and_keeps_every_email(db, monkeypatch):
    rows = [_email(db) for _ in range(3)]
    _run_send(db, monkeypatch, GmailAuthError("Gmail auth expired, must reconnect"))
    campaign = db.get(Campaign, 10)
    assert (campaign.status, campaign.pause_reason, campaign.paused_by) == ("paused", "gmail_auth", "system")
    assert {db.get(EmailSent, r.id).status for r in rows} == {"queued"}
    assert _used(db) == 10  # nothing released: the work is still owed


def test_transient_error_retries_later(db, monkeypatch):
    row = _email(db)
    _run_send(db, monkeypatch, GmailTransientError("Gmail send failed: 503"))
    row = db.get(EmailSent, row.id)
    assert row.status == "queued" and row.scheduled_at > datetime.utcnow()
    assert db.get(Campaign, 10).status == "running"


def test_permanent_failure_returns_the_credit(db, monkeypatch):
    row = _email(db)
    _run_send(db, monkeypatch, GmailSendError("Gmail send failed: 400", 400))
    assert db.get(EmailSent, row.id).status == "failed"
    assert db.get(EmailSent, row.id).status_changed_at is not None
    assert _used(db) == 9
    assert db.get(Campaign, 10).credits_released == 1
    assert db.query(CreditLedger).filter_by(reason=credits.RELEASE_SEND_FAILED).count() == 1


def test_null_body_goes_back_to_generation_not_to_failed(db, monkeypatch):
    row = _email(db, body=None)
    _run_send(db, monkeypatch, AssertionError("must not be sent"))
    row = db.get(EmailSent, row.id)
    assert (row.status, row.subject) == ("pending_enrichment", None)
    assert _used(db) == 10


# ── which failures return a credit ───────────────────────────────────────────

@pytest.mark.parametrize("kw,replaced,released", [
    ({}, False, 1),                                    # paid first touch, nothing filled it
    ({}, True, 0),                                     # a replacement lead took the slot (P33)
    ({"followup_number": 1}, False, 0),               # follow-ups were never charged
    ({"is_test": True}, False, 0),                     # test sends were never charged
    ({"replacement_reason": "bounce"}, False, 0),      # bounced original was delivered
    ({"replacement_reason": "enrichment_exhausted"}, False, 1),
])
def test_release_rules(db, kw, replaced, released):
    row = _email(db, **kw)
    assert outcomes.fail(db, row, "x", replaced=replaced) == released
    db.commit()
    assert _used(db) == 10 - released


def test_campaigns_from_before_the_ledger_never_auto_release(db):
    campaign = db.get(Campaign, 10)
    campaign.credits_reserved = None
    db.commit()
    assert outcomes.fail(db, _email(db), "x") == 0
    assert _used(db) == 10


def test_releases_never_exceed_what_the_campaign_reserved(db):
    db.get(Campaign, 10).credits_reserved = 2
    db.commit()
    total = sum(outcomes.fail(db, _email(db), "x") for _ in range(5))
    db.commit()
    assert total == 2 and _used(db) == 8


# ── finishing, cancelling, resuming ─────────────────────────────────────────

def test_completion_closes_the_order(db):
    _email(db, status="sent")
    campaign_worker._check_campaign_completion(db)
    assert db.get(Campaign, 10).status == "completed"
    assert db.get(OutreachOrder, 100).status == "completed"


def test_expiry_retires_unsent_work_and_returns_its_credits(db):
    _email(db, status="sent")
    unsent = [_email(db) for _ in range(4)]
    fu = _email(db, status="followup_pending", followup_number=1)
    campaign = db.get(Campaign, 10)
    campaign.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    campaign_worker._check_campaign_completion(db)
    assert {db.get(EmailSent, r.id).status for r in unsent} == {"expired"}
    assert db.get(EmailSent, fu.id).status == "cancelled_expired"
    assert _used(db) == 6
    assert db.get(OutreachOrder, 100).status == "completed"


def test_cancel_settles_every_unsent_paid_slot(db):
    for status in ("queued", "pending_enrichment", "pending_enrichment"):
        _email(db, status=status)
    released = campaign_worker.finish_campaign(db, db.get(Campaign, 10), reason="t",
                                               final_status="cancelled")
    assert released == 3 and _used(db) == 7
    assert db.get(Campaign, 10).status == "cancelled"
    assert db.query(CreditLedger).filter_by(reason=credits.RELEASE_CAMPAIGN_CANCELLED).count() == 1


def test_resume_replans_from_now_without_adding_the_pause(db, monkeypatch):
    from services.email_campaign import campaign_service
    row = _email(db)
    campaign = db.get(Campaign, 10)
    campaign.status = "paused"
    campaign.paused_at = datetime.utcnow() - timedelta(days=22)
    campaign.pause_reason, campaign.paused_by = "user", "user"
    db.commit()

    def plan_from_now(db_, campaign_id):
        for e in db_.query(EmailSent).filter_by(campaign_id=campaign_id, status="queued"):
            e.scheduled_at = datetime.utcnow() + timedelta(minutes=2)
        db_.commit()
    monkeypatch.setattr(campaign_worker, "compute_campaign_schedule", plan_from_now)
    campaign_service.transition_campaign(db, 10, "running")
    row = db.get(EmailSent, row.id)
    assert row.scheduled_at < datetime.utcnow() + timedelta(hours=1)  # not 22 days out
    campaign = db.get(Campaign, 10)
    assert (campaign.pause_reason, campaign.paused_at) == (None, None)


def test_pause_records_who_and_why(db):
    from services.email_campaign import campaign_service
    campaign_service.transition_campaign(db, 10, "paused")
    campaign = db.get(Campaign, 10)
    assert (campaign.paused_by, campaign.pause_reason) == ("user", "user")


def test_reconnecting_gmail_resumes_what_the_system_paused(db, monkeypatch):
    from api import routes_gmail
    monkeypatch.setattr(campaign_worker, "compute_campaign_schedule", lambda db_, cid: None)
    _email(db)
    campaign = db.get(Campaign, 10)
    campaign.status, campaign.pause_reason, campaign.paused_by = "paused", "gmail_auth", "system"
    campaign.paused_at = datetime.utcnow()
    db.commit()
    routes_gmail._resume_auth_paused_campaigns(db, "u", "u@gmail.com")
    assert db.get(Campaign, 10).status == "running"


def test_reconnecting_gmail_leaves_a_users_own_pause_alone(db, monkeypatch):
    from api import routes_gmail
    _email(db)
    campaign = db.get(Campaign, 10)
    campaign.status, campaign.pause_reason, campaign.paused_by = "paused", "user", "user"
    db.commit()
    routes_gmail._resume_auth_paused_campaigns(db, "u", "u@gmail.com")
    assert db.get(Campaign, 10).status == "paused"
