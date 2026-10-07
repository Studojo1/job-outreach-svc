"""Ticket #45: a second campaign must not cold-email people the first one
already wrote to.

Runs production code against SQLite: create_campaign and the worker's
_send_ready. Only Gmail is stubbed.
"""
import pathlib
import sys
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import (
    Base, Campaign, Candidate, CreditLedger, EmailAccount, EmailSent, Lead, LeadScore, OutreachOrder,
    PaymentOrder, SuppressedEmail, User, UserCredit,
)
from services.email_campaign import campaign_worker
from services.email_campaign.campaign_service import create_campaign
from services.email_campaign.contacted import ALREADY_CONTACTED_MESSAGE


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


NOW = datetime(2026, 10, 7, 12, 0, 0)
PRATEEK = "prateek.kushwah@refyne.co.in"


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (
        User, Candidate, Lead, LeadScore, Campaign, EmailAccount, EmailSent, OutreachOrder, PaymentOrder,
        UserCredit, CreditLedger, SuppressedEmail)])
    S = sessionmaker(bind=engine)
    monkeypatch.setattr("database.session.SessionLocal", S)
    s = S()
    s.add_all([
        User(id="u", email="u@x.com", name="U", email_verified=True, created_at=NOW, updated_at=NOW),
        User(id="v", email="v@x.com", name="V", email_verified=True, created_at=NOW, updated_at=NOW),
        Candidate(id=1, user_id="u", resume_text="."),
        Candidate(id=2, user_id="v", resume_text="."),
        EmailAccount(id=5, user_id="u", email_address="u@gmail.com", provider="gmail",
                     access_token="t", refresh_token="r", token_expiry=datetime.utcnow() + timedelta(days=1)),  # noqa: S106
        EmailAccount(id=6, user_id="v", email_address="v@gmail.com", provider="gmail",
                     access_token="t", refresh_token="r", token_expiry=datetime.utcnow() + timedelta(days=1)),  # noqa: S106
        UserCredit(user_id="u", total_credits=200, used_credits=200),
        UserCredit(user_id="v", total_credits=200, used_credits=200),
        Lead(id=101, candidate_id=1, name="Prateek", company="Refyne", email=PRATEEK, email_verified=True),
        Lead(id=102, candidate_id=1, name="Meghna", company="GMI", email="meghna@gmi.ai", email_verified=True),
        Lead(id=103, candidate_id=1, name="New", company="Fresh", email="new@fresh.io", email_verified=True),
        # The first campaign: already wrote to Prateek and Meghna, then cancelled.
        Campaign(id=142, candidate_id=1, email_account_id=5, name="first", status="cancelled", daily_limit=20,
                 credits_reserved=3, credits_released=1),
        EmailSent(id=1, campaign_id=142, lead_id=101, to_email=PRATEEK, subject="s", body="b",
                  status="sent", sent_at=NOW - timedelta(days=6), enrichment_status="enriched"),
        EmailSent(id=2, campaign_id=142, lead_id=102, to_email="meghna@gmi.ai", subject="s", body="b",
                  status="sent", sent_at=NOW - timedelta(days=6), enrichment_status="enriched"),
    ])
    s.commit()
    yield s
    s.close()


def _mock_send(monkeypatch, sent):
    def fake(**kw):
        sent.append(kw["to_email"])
        return {"id": f"m{len(sent)}", "threadId": f"t{len(sent)}"}
    monkeypatch.setattr(campaign_worker, "send_gmail_email", fake)
    monkeypatch.setattr(campaign_worker, "_ensure_tracking_token", lambda e: None)
    monkeypatch.setattr(campaign_worker, "ph_capture", lambda *a, **k: None)
    monkeypatch.setattr(campaign_worker, "_deferred_to_send_window", lambda *a: None)
    monkeypatch.setattr("services.email_campaign.gmail_send_service.fetch_message_id_header", lambda *a: None)


def _queued(db, id_, campaign_id, to_email, **kw):
    db.add(EmailSent(id=id_, campaign_id=campaign_id, to_email=to_email, subject="s", body="b", status="queued",
                     scheduled_at=datetime.utcnow() - timedelta(minutes=1), enrichment_status="enriched", **kw))


def test_a_new_campaign_leaves_out_people_already_emailed(db):
    result = create_campaign(db, user_id="u", name="second", email_account_id=5, candidate_id=1,
                             selected_styles=["ai"])
    db.commit()
    rows = db.query(EmailSent).filter(EmailSent.campaign_id == result.get("campaign_id", result.get("id"))).all()
    assert [r.lead_id for r in rows] == [103]


def test_the_lead_limit_is_spent_on_new_people(db):
    result = create_campaign(db, user_id="u", name="second", email_account_id=5, candidate_id=1,
                             selected_styles=["ai"], lead_limit=1)
    db.commit()
    rows = db.query(EmailSent).filter(EmailSent.campaign_id == result.get("campaign_id", result.get("id"))).all()
    assert [r.lead_id for r in rows] == [103]


def test_the_worker_refuses_a_repeat_first_touch_and_returns_the_credit(db, monkeypatch):
    sent = []
    _mock_send(monkeypatch, sent)
    db.add(Campaign(id=145, candidate_id=1, email_account_id=5, name="second", status="running",
                    daily_limit=20, credits_reserved=200, credits_released=0))
    _queued(db, 10, 145, " Prateek.Kushwah@Refyne.co.in", lead_id=101)
    db.commit()
    campaign_worker._send_ready(db)
    assert sent == []
    e = db.get(EmailSent, 10)
    assert e.status == "failed" and e.error_message == ALREADY_CONTACTED_MESSAGE
    assert db.query(UserCredit).filter_by(user_id="u").one().used_credits == 199


def test_new_people_still_go_out(db, monkeypatch):
    sent = []
    _mock_send(monkeypatch, sent)
    db.add(Campaign(id=145, candidate_id=1, email_account_id=5, name="second", status="running",
                    daily_limit=20, credits_reserved=200, credits_released=0))
    _queued(db, 10, 145, "new@fresh.io", lead_id=103)
    db.commit()
    campaign_worker._send_ready(db)
    assert sent == ["new@fresh.io"]


def test_another_user_may_email_the_same_person(db, monkeypatch):
    sent = []
    _mock_send(monkeypatch, sent)
    db.add(Campaign(id=200, candidate_id=2, email_account_id=6, name="theirs", status="running",
                    daily_limit=20, credits_reserved=200, credits_released=0))
    _queued(db, 20, 200, PRATEEK)
    db.commit()
    campaign_worker._send_ready(db)
    assert sent == [PRATEEK]


def test_a_test_email_does_not_count_as_contact(db, monkeypatch):
    sent = []
    _mock_send(monkeypatch, sent)
    db.query(EmailSent).filter(EmailSent.id == 1).update({"is_test": True})
    db.add(Campaign(id=145, candidate_id=1, email_account_id=5, name="second", status="running",
                    daily_limit=20, credits_reserved=200, credits_released=0))
    _queued(db, 10, 145, PRATEEK, lead_id=101)
    db.commit()
    campaign_worker._send_ready(db)
    assert sent == [PRATEEK]
