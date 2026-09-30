"""Campaign mail goes out without the open-tracking pixel.

Ticket #40: a user's outreach landed in recipients' Spam. Sent between two of
our own Gmail accounts with the production send code, mail carrying the hidden
1x1 pixel went to Spam every time; the same body as plain text, or as HTML
without the pixel, went to the Inbox. Authentication passed in every case.

These tests drive the real send loop with only the Gmail call stubbed, and
assert on what would have been handed to Gmail.
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

from database.models import (  # noqa: E402
    Base, Campaign, Candidate, CreditLedger, EmailAccount, EmailSent, Lead, LeadScore,
    OutreachOrder, PaymentOrder, SuppressedEmail, User, UserCredit,
)
from services.email_campaign import campaign_worker, gmail_send_service  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


NOW = datetime(2026, 9, 30, 12, 0, 0)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (
        User, Candidate, Lead, LeadScore, Campaign, EmailAccount, EmailSent, OutreachOrder,
        PaymentOrder, UserCredit, CreditLedger, SuppressedEmail)])
    s = sessionmaker(bind=engine)()
    s.add_all([
        User(id="u", email="u@x.com", name="U", email_verified=True, created_at=NOW, updated_at=NOW),
        Candidate(id=1, user_id="u", resume_text="."),
        EmailAccount(id=5, user_id="u", email_address="u@gmail.com", provider="gmail",
                     access_token="t", refresh_token="r", token_expiry=datetime.utcnow() + timedelta(days=1)),  # noqa: S106
        UserCredit(user_id="u", total_credits=200, used_credits=200),
        Campaign(id=10, candidate_id=1, email_account_id=5, name="a", status="running", daily_limit=20,
                 user_timezone="Asia/Kolkata", credits_reserved=3, credits_released=0),
    ])
    s.commit()
    yield s
    s.close()


def test_first_touch_is_sent_without_a_pixel(db, monkeypatch):
    calls = []

    def fake(**kw):
        calls.append(kw)
        return {"id": "m1", "threadId": "t1"}

    monkeypatch.setattr(campaign_worker, "send_gmail_email", fake)
    monkeypatch.setattr(campaign_worker, "ph_capture", lambda *a, **k: None)
    monkeypatch.setattr(campaign_worker, "_deferred_to_send_window", lambda c, now: None)
    db.add(EmailSent(campaign_id=10, to_email="a@x.com", subject="s", body="b", status="queued",
                     scheduled_at=datetime.utcnow() - timedelta(minutes=1)))
    db.commit()

    campaign_worker._send_ready(db)

    assert len(calls) == 1
    assert calls[0].get("pixel_url") is None
    first = db.query(EmailSent).filter_by(followup_number=0).one()
    assert first.status == "sent"
    assert first.tracking_token is None


def test_no_pixel_means_a_plain_text_message(monkeypatch):
    """With no pixel the MIME message is text/plain, with no HTML part and no <img>."""
    captured = {}

    class Resp:
        ok = True
        status_code = 200
        def json(self):
            return {"id": "m", "threadId": "t"}

    def fake_post(url, json, headers, timeout):
        captured["raw"] = json["raw"]
        return Resp()

    monkeypatch.setattr(gmail_send_service.requests, "post", fake_post)
    from services.email_campaign import suppression
    monkeypatch.setattr(suppression, "guard_send", lambda *a, **k: None)
    gmail_send_service.send_gmail_email(access_token="t", to_email="a@x.com", subject="s",  # noqa: S106
                                        body="Hi\n\nBody", from_email="u@gmail.com", pixel_url=None)
    import base64
    raw = base64.urlsafe_b64decode(captured["raw"]).decode()
    assert "text/plain" in raw
    assert "text/html" not in raw and "<img" not in raw


def test_open_tracking_stays_off():
    """Turning it back on sends campaign mail to Spam; that must be a deliberate change."""
    assert campaign_worker.TRACK_OPENS is False
