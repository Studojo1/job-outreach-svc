"""B2C audit 29 Sep 2026: sending and reply rows.

PS-N16 (9-5 send window), NEW-03 / PS-N03 / PS-N14 (reply checks cover
paused campaigns, page the inbox, skip dead mailboxes, one check per
mailbox), PS-N18 (restart), PP-P26 (paid-credit cap), PP-P36 (stuck
'sending' reaper). Every test drives production code.
"""
import pathlib
import sys
from datetime import datetime, timedelta

import pytest
import pytz
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import (  # noqa: E402
    Base, Campaign, Candidate, CreditLedger, EmailAccount, EmailSent, Lead, LeadScore,
    OutreachOrder, PaymentOrder, SuppressedEmail, User, UserCredit,
)
from services.email_campaign import campaign_worker, gmail_inbox_service  # noqa: E402

IST = pytz.timezone("Asia/Kolkata")


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


NOW = datetime(2026, 9, 29, 12, 0, 0)


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


def _ist(hour, minute=0):
    return IST.localize(datetime(2026, 9, 29, hour, minute)).astimezone(pytz.utc).replace(tzinfo=None)


def _mock_send(monkeypatch, sent):
    def fake(**kw):
        sent.append(kw["to_email"])
        return {"id": f"m{len(sent)}", "threadId": f"t{len(sent)}"}
    monkeypatch.setattr(campaign_worker, "send_gmail_email", fake)
    monkeypatch.setattr(campaign_worker, "_ensure_tracking_token", lambda e: None)
    monkeypatch.setattr(campaign_worker, "ph_capture", lambda *a, **k: None)


# ── PS-N16: the 9-5 window is enforced at send time ────────────────────────

@pytest.mark.real_send_window
def test_send_window_is_nine_to_five_local(db):
    c = db.get(Campaign, 10)
    assert campaign_worker._deferred_to_send_window(c, _ist(11)) is None
    assert campaign_worker._deferred_to_send_window(c, _ist(16, 59)) is None
    later = campaign_worker._deferred_to_send_window(c, _ist(17, 30))     # 5:30 PM
    assert later is not None
    local = later.replace(tzinfo=pytz.utc).astimezone(IST)
    assert (local.day, local.hour) == (30, 9)
    early = campaign_worker._deferred_to_send_window(c, _ist(6))
    assert early.replace(tzinfo=pytz.utc).astimezone(IST).hour == 9


def test_a_first_touch_due_at_night_waits(db, monkeypatch):
    sent = []
    _mock_send(monkeypatch, sent)
    tomorrow = datetime.utcnow() + timedelta(hours=12)
    monkeypatch.setattr(campaign_worker, "_deferred_to_send_window", lambda c, now: tomorrow)
    db.add(EmailSent(campaign_id=10, to_email="a@x.com", subject="s", body="b", status="queued",
                     scheduled_at=datetime.utcnow() - timedelta(minutes=1)))
    db.commit()
    campaign_worker._send_ready(db)
    assert sent == []
    assert db.query(EmailSent).one().scheduled_at == tomorrow


# ── PP-P26: never more paid first touches than were paid for ─────────────

def test_sends_stop_at_the_paid_credit_count(db, monkeypatch):
    sent = []
    _mock_send(monkeypatch, sent)
    monkeypatch.setattr(campaign_worker, "MAX_SEND_PER_CYCLE", 50)
    for i in range(5):
        db.add(EmailSent(campaign_id=10, to_email=f"l{i}@x.com", subject="s", body="b", status="queued",
                         scheduled_at=datetime.utcnow() - timedelta(minutes=10 - i)))
    db.commit()
    campaign_worker._send_ready(db)
    assert len(sent) == 3                                  # credits_reserved = 3
    left = db.query(EmailSent).filter_by(status="expired").all()
    assert len(left) == 2 and all(e.error_message == campaign_worker.OVER_CAP_MESSAGE for e in left)


def test_bounce_replacements_are_free_and_not_capped(db, monkeypatch):
    sent = []
    _mock_send(monkeypatch, sent)
    for i in range(3):
        db.add(EmailSent(campaign_id=10, to_email=f"s{i}@x.com", status="sent", sent_at=datetime.utcnow()))
    db.add(EmailSent(campaign_id=10, to_email="r@x.com", subject="s", body="b", status="queued",
                     replacement_reason="bounce", scheduled_at=datetime.utcnow() - timedelta(minutes=1)))
    db.commit()
    campaign_worker._send_ready(db)
    assert sent == ["r@x.com"]


def test_legacy_campaign_without_a_reservation_sends_nothing_more(db, monkeypatch):
    # Audit 30 Sep (PP-P26): a reservation of 0 is a cap of 0. Legacy campaigns
    # re-reserve from the wallet when resumed (test_audit_0930_campaign.py).
    sent = []
    _mock_send(monkeypatch, sent)
    db.get(Campaign, 10).credits_reserved = 0
    db.add(EmailSent(campaign_id=10, to_email="a@x.com", subject="s", body="b", status="queued",
                     scheduled_at=datetime.utcnow() - timedelta(minutes=1)))
    db.commit()
    campaign_worker._send_ready(db)
    assert sent == []
    assert db.query(EmailSent).one().error_message == campaign_worker.OVER_CAP_MESSAGE


def test_no_apollo_spend_beyond_the_paid_count(db, monkeypatch):
    calls = []
    monkeypatch.setattr("services.enrichment.enrichment_service.enrich_single_lead_classified",
                        lambda *a, **k: calls.append(1))
    for i in range(3):
        db.add(EmailSent(campaign_id=10, to_email=f"s{i}@x.com", status="sent"))
    db.add(Lead(id=50, candidate_id=1, name="x"))
    row = EmailSent(campaign_id=10, lead_id=50, status="pending_enrichment", enrichment_status="pending")
    db.add(row)
    db.commit()
    assert campaign_worker._enrich_one(db, row) is False
    assert calls == [] and db.get(EmailSent, row.id).status == "expired"


# ── NEW-03 / PS-N14: which mailboxes get reply checks ──────────────────────

def test_paused_and_recently_finished_campaigns_are_checked(db):
    now = datetime.utcnow()
    db.add_all([
        EmailAccount(id=6, user_id="u", email_address="p@gmail.com", access_token="t"),  # noqa: S106
        EmailAccount(id=7, user_id="u", email_address="old@gmail.com", access_token="t"),  # noqa: S106
        EmailAccount(id=8, user_id="u", email_address="new@gmail.com", access_token="t"),  # noqa: S106
        Campaign(id=20, candidate_id=1, email_account_id=6, name="p", status="paused"),
        Campaign(id=21, candidate_id=1, email_account_id=7, name="o", status="completed",
                 completed_at=now - timedelta(days=60)),
        Campaign(id=22, candidate_id=1, email_account_id=8, name="n", status="cancelled",
                 completed_at=now - timedelta(days=3)),
    ])
    db.commit()
    assert set(campaign_worker._reply_check_account_ids(db, now)) == {5, 6, 8}


def test_dead_and_recently_checked_mailboxes_are_skipped(db, monkeypatch):
    checked = []
    monkeypatch.setattr(campaign_worker, "check_mailbox_replies",
                        lambda db_, account, after_epoch=None: checked.append(account.id) or (0, 0))
    monkeypatch.setattr(campaign_worker, "_last_reply_check", 0.0)
    now = datetime.utcnow()
    db.add_all([
        EmailAccount(id=6, user_id="u", email_address="dead@gmail.com", access_token="t",  # noqa: S106
                     token_invalid_at=now),
        EmailAccount(id=7, user_id="u", email_address="fresh@gmail.com", access_token="t",  # noqa: S106
                     last_reply_check_at=now - timedelta(seconds=30)),
        Campaign(id=20, candidate_id=1, email_account_id=6, name="d", status="running"),
        Campaign(id=21, candidate_id=1, email_account_id=7, name="f", status="running"),
    ])
    db.commit()
    campaign_worker._check_replies(db)
    assert checked == [5]


# ── NEW-03 / PS-N03: inbox listing pages, and a failure is not "no mail" ──

class _Resp:
    def __init__(self, ok, payload):
        self.ok, self._p, self.status_code, self.text = ok, payload, 200 if ok else 500, ""

    def json(self):
        return self._p


def test_inbox_listing_follows_every_page(monkeypatch):
    pages = [
        _Resp(True, {"messages": [{"id": "1", "threadId": "a"}], "nextPageToken": "p2"}),
        _Resp(True, {"messages": [{"id": "2", "threadId": "b"}]}),
    ]
    monkeypatch.setattr(gmail_inbox_service.requests, "get", lambda *a, **k: pages.pop(0))
    assert [m["id"] for m in gmail_inbox_service.list_inbox_messages("tok", 0)] == ["1", "2"]


def test_failed_inbox_listing_does_not_advance_the_check_time(db, monkeypatch):
    monkeypatch.setattr(gmail_inbox_service.requests, "get", lambda *a, **k: _Resp(False, {}))
    monkeypatch.setattr(campaign_worker, "_refresh_token_sync", lambda a, d: "tok")
    account = db.get(EmailAccount, 5)
    assert account.last_reply_check_at is None
    campaign_worker.check_mailbox_replies(db, account)
    assert db.get(EmailAccount, 5).last_reply_check_at is None


# ── NEW-03 / PS-N16 / PS-N18: resume and restart ───────────────────────────

def test_resume_reads_replies_first_and_replans_in_business_hours(db, monkeypatch):
    from services.email_campaign import campaign_service
    calls = []
    monkeypatch.setattr(campaign_worker, "check_mailbox_replies",
                        lambda db_, account, after_epoch=None: calls.append(("scan", after_epoch)) or (0, 0))
    monkeypatch.setattr(campaign_worker, "compute_campaign_schedule",
                        lambda db_, cid, resume=False: calls.append(("plan", resume)))
    c = db.get(Campaign, 10)
    c.status = "paused"
    first = datetime.utcnow() - timedelta(days=9)
    db.add(EmailSent(campaign_id=10, to_email="a@x.com", status="sent", sent_at=first))
    db.add(EmailSent(campaign_id=10, to_email="b@x.com", status="queued"))
    db.commit()
    campaign_service.transition_campaign(db, 10, "running")
    assert calls[0] == ("scan", int(first.timestamp()))
    assert ("plan", True) in calls


def test_restart_waits_for_morning_and_renews_an_expired_plan(db, monkeypatch):
    from services.email_campaign import campaign_service
    monkeypatch.setattr(campaign_worker, "check_mailbox_replies", lambda *a, **k: (0, 0))
    monkeypatch.setattr(campaign_worker, "compute_campaign_schedule", lambda *a, **k: None)
    now = datetime.utcnow()
    c = db.get(Campaign, 10)
    c.status = "cancelled"
    c.started_at = now - timedelta(days=20)
    c.expires_at = now - timedelta(days=12)                       # an 8-day plan, ran out
    db.query(UserCredit).update({"used_credits": 0})
    parent = EmailSent(campaign_id=10, to_email="a@x.com", status="sent", sent_at=now - timedelta(days=20))
    db.add(parent)
    db.flush()
    db.add(EmailSent(campaign_id=10, to_email="b@x.com", status="expired", followup_number=0))
    db.add(EmailSent(campaign_id=10, to_email="a@x.com", status="cancelled_expired", followup_number=1,
                     parent_email_id=parent.id, scheduled_at=now - timedelta(days=15)))
    db.commit()
    campaign_service.transition_campaign(db, 10, "running")
    fu = db.query(EmailSent).filter_by(followup_number=1).one()
    assert fu.status == "followup_pending"
    local = fu.scheduled_at.replace(tzinfo=pytz.utc).astimezone(IST)
    assert fu.scheduled_at > now and local.hour == 9
    assert db.get(Campaign, 10).expires_at > now + timedelta(days=7)


# ── PP-P36: stuck 'sending' rows ───────────────────────────────────────────

@pytest.mark.parametrize("found,expected", [({"id": "g1", "threadId": "t1"}, "sent"), ({}, "queued"),
                                            (None, "sending")])
def test_stuck_sending_is_settled_from_the_sent_folder(db, monkeypatch, found, expected):
    from services.email_campaign import gmail_send_service
    monkeypatch.setattr(campaign_worker, "_refresh_token_sync", lambda a, d: "tok")
    monkeypatch.setattr(gmail_send_service, "find_sent_message", lambda tok, to, after: found)
    db.add(EmailSent(id=1, campaign_id=10, to_email="a@x.com", status="sending",
                     status_changed_at=datetime.utcnow() - timedelta(minutes=30)))
    db.commit()
    campaign_worker._reap_stuck_sending(db)
    row = db.get(EmailSent, 1)
    assert row.status == expected
    if expected == "sent":
        assert row.thread_id == "t1"


def test_a_send_in_flight_is_not_reaped(db, monkeypatch):
    from services.email_campaign import gmail_send_service
    monkeypatch.setattr(gmail_send_service, "find_sent_message",
                        lambda *a: pytest.fail("must not look up a fresh send"))
    db.add(EmailSent(id=1, campaign_id=10, to_email="a@x.com", status="sending",
                     status_changed_at=datetime.utcnow() - timedelta(minutes=2)))
    db.commit()
    assert campaign_worker._reap_stuck_sending(db) == 0


def test_mailbox_without_read_scope_is_marked_not_retried(db, monkeypatch):
    # Prod 29 Sep: "403 insufficient authentication scopes" every 5 minutes.
    monkeypatch.setattr(gmail_inbox_service.requests, "get",
                        lambda *a, **k: type("R", (), {"ok": False, "status_code": 403,
                                                       "text": '{"error": "Request had insufficient authentication scopes."}'})())
    monkeypatch.setattr(campaign_worker, "_refresh_token_sync", lambda a, d: "tok")
    monkeypatch.setattr(campaign_worker, "_last_reply_check", 0.0)
    campaign_worker._check_replies(db)
    assert db.get(EmailAccount, 5).token_invalid_at is not None
