"""Customers hear about their campaign (audit P09/P38/P42/P20)."""
import pathlib
import sys
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import Base, Campaign, CampaignNotice, Candidate, EmailSent, User
from services import campaign_notices


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


NOW = datetime(2026, 10, 5, 12, 0, 0)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (User, Candidate, Campaign, EmailSent, CampaignNotice)])
    s = sessionmaker(bind=engine)()
    s.add_all([User(id="u", email="u@x.com", name="Asha K", email_verified=True, created_at=NOW, updated_at=NOW),
               Candidate(id=1, user_id="u", resume_text=".")])
    s.commit()
    yield s
    s.close()


@pytest.fixture()
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(campaign_notices, "_send", lambda payload: calls.append(payload) or True)
    return calls


def _campaign(db, cid, status, unsent=0, sent_at=None, **kw):
    db.add(Campaign(id=cid, candidate_id=1, name="c", status=status, created_at=NOW - timedelta(days=30), **kw))
    for _ in range(unsent):
        db.add(EmailSent(campaign_id=cid, status="queued"))
    if sent_at:
        db.add(EmailSent(campaign_id=cid, status="sent", sent_at=sent_at))
    db.commit()


def _templates(sent):
    return [c["template"] for c in sent]


def test_dead_mailbox_gets_one_reconnect_email(db, sent):
    _campaign(db, 1, "paused", unsent=40, pause_reason="gmail_auth", paused_at=NOW - timedelta(hours=2))
    campaign_notices.run(db, NOW)
    campaign_notices.run(db, NOW + timedelta(hours=1))
    assert _templates(sent) == ["outreach-gmail-reconnect"]
    assert sent[0]["action_url"].endswith("/connect/gmail") and sent[0]["user_name"] == "Asha"


def test_paused_a_week_with_work_gets_a_reminder(db, sent):
    _campaign(db, 1, "paused", unsent=10, pause_reason="user", paused_at=NOW - timedelta(days=8))
    campaign_notices.run(db, NOW)
    assert _templates(sent) == ["outreach-campaign-paused"]


def test_recent_pause_is_left_alone(db, sent):
    _campaign(db, 1, "paused", unsent=10, pause_reason="user", paused_at=NOW - timedelta(days=2))
    campaign_notices.run(db, NOW)
    assert sent == []


def test_very_old_pause_goes_to_the_founders_once_not_the_customer(db, sent):
    _campaign(db, 1, "paused", unsent=10, paused_at=NOW - timedelta(days=120))
    campaign_notices.run(db, NOW)
    campaign_notices.run(db, NOW + timedelta(hours=1))
    assert _templates(sent) == ["ops-alert", "ops-alert"]  # two founders, first run only
    assert "campaign 1" in sent[0]["message"]


def test_stalled_campaign_tells_customer_and_founders(db, sent):
    _campaign(db, 1, "running", unsent=30, sent_at=NOW - timedelta(days=4))
    campaign_notices.run(db, NOW)
    assert "outreach-campaign-stalled" in _templates(sent)
    assert any(c["template"] == "ops-alert" and "STALLED" in c["message"] for c in sent)


def test_healthy_running_campaign_is_quiet(db, sent):
    _campaign(db, 1, "running", unsent=30, sent_at=NOW - timedelta(hours=5))
    campaign_notices.run(db, NOW)
    assert sent == []


def test_finished_campaign_reports_the_real_split(db, sent):
    _campaign(db, 1, "completed", completed_at=NOW - timedelta(hours=1), credits_released=12)
    for status in ["sent"] * 5 + ["failed"] * 3:
        db.add(EmailSent(campaign_id=1, status=status))
    db.commit()
    campaign_notices.run(db, NOW)
    [n] = sent
    assert (n["template"], n["delivered"], n["total"], n["credits"]) == ("outreach-campaign-finished", 5, 8, 12)


def test_campaigns_finished_before_notices_existed_are_not_emailed(db, sent):
    _campaign(db, 1, "completed", completed_at=datetime(2026, 9, 1))
    campaign_notices.run(db, NOW)
    assert sent == []


def test_failed_send_is_retried_next_hour(db, monkeypatch):
    monkeypatch.setattr(campaign_notices, "_send", lambda p: False)
    _campaign(db, 1, "paused", unsent=5, pause_reason="gmail_auth", paused_at=NOW)
    campaign_notices.run(db, NOW)
    assert db.query(CampaignNotice).count() == 0
