"""Privacy Policy v2.0 §5 and Terms §6: one suppression list, checked by hash
on every path, and the third-party removal request flow.

Runs production code against SQLite: the campaign worker, extension
send-one and contact-check, Apollo enrichment, the Gmail send guard, the
reply check, and the admin endpoints (called as functions). Only Gmail,
Apollo and the classifier are stubbed.
"""
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import (
    Base, Campaign, Candidate, CreditLedger, EmailAccount, EmailSent, Lead, LeadScore, OutreachOrder,
    PaymentOrder, RemovalRequest, SuppressedEmail, User, UserCredit,
)
from services.email_campaign import campaign_worker, suppression


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


NOW = datetime(2026, 9, 29, 12, 0, 0)
ADDR = "Priya@Zomato.com"


@pytest.fixture()
def factory(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (
        User, Candidate, Lead, LeadScore, Campaign, EmailAccount, EmailSent, OutreachOrder, PaymentOrder,
        UserCredit, CreditLedger, SuppressedEmail, RemovalRequest)])
    with engine.begin() as c:
        c.execute(text("CREATE TABLE api_enrich_cache (linkedin_url TEXT PRIMARY KEY, status TEXT, result JSON)"))
        c.execute(text("CREATE TABLE apollo_reveals (rid TEXT PRIMARY KEY, linkedin_url TEXT, apollo_id TEXT,"
                       " phone TEXT, status TEXT)"))
    S = sessionmaker(bind=engine)
    # guard_send and enrichment open their own sessions
    monkeypatch.setattr("database.session.SessionLocal", S)
    return S


@pytest.fixture()
def db(factory):
    s = factory()
    s.add_all([
        User(id="u", email="u@x.com", name="U", email_verified=True, created_at=NOW, updated_at=NOW),
        Candidate(id=1, user_id="u", resume_text="."),
        EmailAccount(id=5, user_id="u", email_address="u@gmail.com", provider="gmail",
                     access_token="t", refresh_token="r", token_expiry=datetime.utcnow() + timedelta(days=1)),  # noqa: S106
        UserCredit(user_id="u", total_credits=200, used_credits=200),
        Campaign(id=10, candidate_id=1, email_account_id=5, name="a", status="running", daily_limit=20,
                 credits_reserved=200, credits_released=0),
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
    monkeypatch.setattr("services.email_campaign.gmail_send_service.fetch_message_id_header", lambda *a: None)


def _hash_only(db, address, source="removal_request"):
    """What remains after a removal request is done: the hash, no address."""
    suppression.suppress(db, address, "test", source=source)
    suppression.forget_plaintext(db, address)
    db.commit()
    row = db.query(SuppressedEmail).one()
    assert row.email is None and row.email_hash == suppression.email_hash(address)


# ── the list itself ─────────────────────────────────────────────────────────

def test_hash_is_of_the_normalised_address():
    import hashlib
    assert suppression.email_hash("  Priya@Zomato.COM ") == hashlib.sha256(b"priya@zomato.com").hexdigest()
    assert suppression.mask("priya@zomato.com") == "p•••@zomato.com"


def test_hash_only_entry_still_blocks(db):
    _hash_only(db, ADDR)
    assert suppression.is_suppressed(db, "priya@zomato.com")
    assert suppression.is_suppressed(db, " PRIYA@zomato.com")
    assert not suppression.is_suppressed(db, "someone@zomato.com")


def test_first_source_wins_and_it_is_idempotent(db):
    suppression.suppress(db, ADDR, "bounce: 550", source="bounce")
    suppression.suppress(db, ADDR.lower(), "asked", source="removal_request")
    db.commit()
    [row] = db.query(SuppressedEmail).all()
    assert row.source == "bounce" and row.email == "priya@zomato.com"
    with pytest.raises(ValueError):
        suppression.suppress(db, "a@b.com", "x", source="spam")


# ── every path checks it ─────────────────────────────────────────────────────

def test_worker_first_touch_skips_hash_only_address_and_returns_credit(db, monkeypatch):
    sent = []
    _mock_send(monkeypatch, sent)
    _hash_only(db, ADDR)
    db.add(EmailSent(id=1, campaign_id=10, to_email="priya@zomato.com", subject="s", body="b", status="queued",
                     scheduled_at=datetime.utcnow() - timedelta(minutes=1), enrichment_status="enriched"))
    db.commit()
    campaign_worker._send_ready(db)
    assert sent == []
    e = db.get(EmailSent, 1)
    assert e.status == "failed" and "asked to be removed" in e.error_message
    assert db.query(UserCredit).one().used_credits == 199


def test_worker_follow_up_to_hash_only_address_is_cancelled(db, monkeypatch):
    sent = []
    _mock_send(monkeypatch, sent)
    _hash_only(db, ADDR)
    db.add_all([
        EmailSent(id=1, campaign_id=10, to_email="priya@zomato.com", subject="s", body="b", status="sent",
                  thread_id="T", sent_at=datetime.utcnow() - timedelta(days=5), enrichment_status="enriched"),
        EmailSent(id=2, campaign_id=10, to_email="priya@zomato.com", status="followup_pending", followup_number=1,
                  parent_email_id=1, scheduled_at=datetime.utcnow() - timedelta(minutes=1), enrichment_status="enriched"),
    ])
    db.commit()
    campaign_worker._process_followups(db)
    assert sent == [] and db.get(EmailSent, 2).status == "cancelled_reply"


def test_gmail_send_guard_is_the_last_line(db, monkeypatch):
    from services.email_campaign import gmail_send_service

    _hash_only(db, ADDR)
    posted = []
    monkeypatch.setattr(gmail_send_service.requests, "post", lambda *a, **k: posted.append(a) or pytest.fail("sent"))
    with pytest.raises(suppression.SuppressedAddress):
        gmail_send_service.send_gmail_email(access_token="t", to_email="priya@zomato.com", subject="s", body="b")  # noqa: S106
    assert posted == []


def test_enrichment_never_stores_a_suppressed_address(db, monkeypatch):
    from services.enrichment import enrichment_service as es

    _hash_only(db, ADDR)
    monkeypatch.setattr(es.apollo_keys, "has_valid_key", lambda: True)
    monkeypatch.setattr(es, "_record_apollo_reveal", lambda: None)
    monkeypatch.setattr(es, "apollo_post", lambda *a, **k: SimpleNamespace(
        status_code=200, ok=True,
        json=lambda: {"person": {"email": "priya@zomato.com", "email_status": "verified", "first_name": "Priya"}}))
    r = es.enrich_single_lead_classified(Lead(name="Priya S", company="Zomato"))
    assert not r.success and r.error_type == "no_match" and "suppression" in r.error_detail


def _extension_stubs(monkeypatch, db, lead_email):
    from api import routes_extension as rx

    sent = []
    monkeypatch.setattr(rx, "_sends_disabled", lambda: False)
    monkeypatch.setattr(rx, "_resolve_candidate", lambda db_, uid: db.get(Candidate, 1))
    monkeypatch.setattr(rx, "_resolve_email_account", lambda db_, uid, aid: db.get(EmailAccount, 5))
    monkeypatch.setattr(rx, "_sends_today", lambda db_, uid: 0)
    monkeypatch.setattr(rx, "_suggest_alternatives", lambda req: [])
    monkeypatch.setattr(rx, "_resolve_contact", lambda *a, **k: {
        "name": "Priya S", "title": "Recruiter", "email": lead_email, "found_by_search": True})
    monkeypatch.setattr("services.email_campaign.gmail_send_service.send_email_via_gmail",
                        lambda **kw: sent.append(kw) or True)
    db.query(UserCredit).one().total_credits = 300
    db.commit()
    return rx, sent


def test_extension_send_one_refuses_a_suppressed_address_and_charges_nothing(db, monkeypatch):
    rx, sent = _extension_stubs(monkeypatch, db, "priya@zomato.com")
    _hash_only(db, ADDR)
    req = rx.SendOneRequest(company="Zomato", subject="Hello", body="Hi Priya", contact_name="Priya S")
    with pytest.raises(HTTPException) as exc:
        rx.send_one_email(req, current_user=SimpleNamespace(id="u"), db=db)
    assert exc.value.status_code == 422 and exc.value.detail.startswith("contact_opted_out:")
    assert sent == []
    assert db.query(UserCredit).one().used_credits == 200


def test_extension_send_one_still_sends_to_others(db, monkeypatch):
    rx, sent = _extension_stubs(monkeypatch, db, "rahul@zomato.com")
    _hash_only(db, ADDR)
    req = rx.SendOneRequest(company="Zomato", subject="Hello", body="Hi Rahul", contact_name="Rahul K")
    out = rx.send_one_email(req, current_user=SimpleNamespace(id="u"), db=db)
    assert out.sent and [s["to_email"] for s in sent] == ["rahul@zomato.com"]


def test_extension_contact_check_says_unreachable(db, monkeypatch):
    rx, _ = _extension_stubs(monkeypatch, db, "priya@zomato.com")
    _hash_only(db, ADDR)
    req = rx.ContactCheckRequest(company="Zomato", contact_name="Priya S")
    out = rx.check_contact(req, current_user=SimpleNamespace(id="u"), db=db)
    assert out.status == "unreachable" and "asked not to be contacted" in out.message
    req = rx.ContactCheckRequest(company="Zomato", contact_email="PRIYA@zomato.com")
    assert rx.check_contact(req, current_user=SimpleNamespace(id="u"), db=db).status == "unreachable"


# ── an opt-out reply suppresses ──────────────────────────────────────────────

@pytest.mark.parametrize("body,expected", [
    ("Please remove me from your list.", True),
    ("Unsubscribe", True),
    ("Kindly stop emailing me", True),
    ("Don't email me again", True),
    ("Not interested, we're not hiring.", False),
    ("Sure, let's talk!\n\nOn Mon, Sep 1, Student wrote:\n> unsubscribe", False),
])
def test_asks_removal(body, expected):
    assert suppression.asks_removal(body) is expected


def test_remove_me_reply_suppresses_the_address(db, monkeypatch):
    monkeypatch.setattr(campaign_worker, "_last_reply_check", 0, raising=False)
    monkeypatch.setattr(campaign_worker, "_refresh_token_sync", lambda a, d: "tok")
    monkeypatch.setattr(campaign_worker, "list_inbox_messages", lambda t, e: [{"id": "m1", "threadId": "T"}])
    monkeypatch.setattr(campaign_worker, "get_message_detail", lambda t, i: {
        "from_email": "priya@zomato.com", "body_text": "Please remove me from your list.",
        "internal_date": NOW.timestamp()})
    monkeypatch.setattr(campaign_worker, "classify_reply_sentiment", lambda b: {"sentiment": "negative"})
    db.add(EmailSent(id=1, campaign_id=10, to_email="Priya@zomato.com", status="sent", thread_id="T", sent_at=NOW))
    db.commit()
    campaign_worker._check_replies(db)
    assert db.get(EmailSent, 1).status == "replied"
    row = db.query(SuppressedEmail).one()
    assert row.source == "reply" and suppression.is_suppressed(db, ADDR)


# ── removal requests and the admin endpoints ─────────────────────────────────

ADMIN = SimpleNamespace(id="admin1", email="admin@studojo.com")


def test_removal_request_flow(db):
    from api import routes_privacy_admin as pa

    db.add_all([
        Lead(id=1, candidate_id=1, name="Priya S", title="Recruiter", company="Zomato", email="Priya@Zomato.com",
             email_verified=True, linkedin_url="https://www.linkedin.com/in/Priya-S/", location="Gurugram, India",
             status="enriched"),
        Lead(id=2, candidate_id=1, name="Rahul", company="Zomato", email="rahul@zomato.com", email_verified=True),
        EmailSent(id=1, campaign_id=10, lead_id=1, to_email="priya@zomato.com", subject="Hi", body="b",
                  status="replied", reply_text="thanks but no", sent_at=NOW),
        EmailSent(id=2, campaign_id=10, lead_id=1, to_email="priya@zomato.com", status="queued",
                  subject="s", body="b", enrichment_status="enriched", scheduled_at=NOW),
        EmailSent(id=3, campaign_id=10, lead_id=2, to_email="rahul@zomato.com", status="sent", sent_at=NOW),
    ])
    db.execute(text("INSERT INTO api_enrich_cache VALUES ('linkedin.com/in/priya-s', 'ok', :r),"
                    " ('linkedin.com/in/rahul', 'ok', :o)"),
               {"r": '{"emails": {"work": "priya@zomato.com", "personal": null}}',
                "o": '{"emails": {"work": "rahul@zomato.com", "personal": "notpriya@zomato.com"}}'})
    db.execute(text("INSERT INTO apollo_reveals (rid, linkedin_url, apollo_id, status) VALUES"
                    " ('r1', 'linkedin.com/in/priya-s', 'a1', 'done'), ('r2', 'linkedin.com/in/rahul', 'a2', 'done')"))
    db.commit()

    created = pa.create_removal_request(pa.RemovalBody(email=" Priya@Zomato.com", received_on=date(2026, 9, 20)),
                                        admin=ADMIN, db=db)
    # suppressed immediately, deadline 30 days from receipt
    assert created["suppressed"] and suppression.is_suppressed(db, "priya@zomato.com")
    assert created["status"] == "open" and created["email"] == "priya@zomato.com"
    assert created["deadline"].startswith("2026-10-20")
    [item] = pa.list_removal_requests(status="open", admin=ADMIN, db=db)["items"]
    assert item["id"] == created["id"]
    # logging the same address again does not open a second request
    again = pa.create_removal_request(pa.RemovalBody(email="priya@zomato.com", received_on=date(2026, 9, 21)),
                                      admin=ADMIN, db=db)
    assert again["id"] == created["id"]

    out = pa.delete_removal_request_data(created["id"], admin=ADMIN, db=db)
    assert out["status"] == "done" and out["email"] is None and out["done_at"]
    assert out["deleted"] == {"emails_cancelled": 1, "leads": 1, "emails_sent": 2,
                              "api_enrich_cache": 1, "apollo_reveals": 1}

    lead = db.get(Lead, 1)
    assert (lead.email, lead.name, lead.linkedin_url, lead.location, lead.email_verified) == (
        None, "Removed contact", None, None, False)
    assert db.get(Lead, 2).email == "rahul@zomato.com"
    e1, e2 = db.get(EmailSent, 1), db.get(EmailSent, 2)
    assert (e1.to_email, e1.reply_text, e1.status) == (None, None, "replied")  # history kept, person gone
    assert e2.status == "failed" and db.query(UserCredit).one().used_credits == 199  # queued one stopped, credit back
    assert db.get(EmailSent, 3).to_email == "rahul@zomato.com"
    assert [r[0] for r in db.execute(text("SELECT linkedin_url FROM api_enrich_cache")).all()] == ["linkedin.com/in/rahul"]
    assert [r[0] for r in db.execute(text("SELECT rid FROM apollo_reveals")).all()] == ["r2"]

    # only the hash is kept, and it still blocks
    row = db.query(SuppressedEmail).one()
    assert row.email is None and row.source == "removal_request"
    assert db.query(RemovalRequest).one().email is None
    assert suppression.is_suppressed(db, "PRIYA@zomato.com")
    assert "priya@zomato.com" not in str([tuple(r) for r in db.execute(text("SELECT * FROM suppressed_emails"))])
    assert "priya@zomato.com" not in str([tuple(r) for r in db.execute(text("SELECT * FROM removal_requests"))])

    with pytest.raises(HTTPException) as exc:
        pa.delete_removal_request_data(created["id"], admin=ADMIN, db=db)
    assert exc.value.status_code == 409
    assert pa.list_removal_requests(status="open", admin=ADMIN, db=db)["items"] == []
    assert len(pa.list_removal_requests(status="done", admin=ADMIN, db=db)["items"]) == 1
    assert len(pa.list_removal_requests(status="all", admin=ADMIN, db=db)["items"]) == 1


def test_suppression_list_endpoint_masks_and_searches(db):
    from api import routes_privacy_admin as pa

    suppression.suppress(db, "bounced@corp.com", "bounce: 550", source="bounce")
    db.commit()
    added = pa.add_suppression(pa.EmailBody(email="Asha@Zomato.com"), admin=ADMIN, db=db)
    assert added["created"] and added["source"] == "manual" and added["email"] == "a•••@zomato.com"
    assert not pa.add_suppression(pa.EmailBody(email="asha@zomato.com"), admin=ADMIN, db=db)["created"]
    _ = pa.create_removal_request(pa.RemovalBody(email="priya@zomato.com", received_on=date(2026, 9, 1)),
                                  admin=ADMIN, db=db)
    suppression.forget_plaintext(db, "priya@zomato.com")
    db.commit()

    out = pa.list_suppression(search="", limit=50, offset=0, admin=ADMIN, db=db)
    assert out["total"] == 3
    by_source = {i["source"]: i for i in out["items"]}
    assert by_source["bounce"]["email"] == "bounced@corp.com"  # bounces shown in full
    assert by_source["manual"]["email"] == "a•••@zomato.com"
    assert by_source["removal_request"]["email"] is None and by_source["removal_request"]["hash_only"]
    assert set(out["items"][0]) >= {"id", "email", "source", "suppressed_at"}

    # a full address finds its hashed-only entry; a fragment finds plaintext ones
    found = pa.list_suppression(search="Priya@Zomato.com", limit=50, offset=0, admin=ADMIN, db=db)
    assert found["total"] == 1 and found["items"][0]["source"] == "removal_request"
    assert pa.list_suppression(search="zomato", limit=50, offset=0, admin=ADMIN, db=db)["total"] == 1
    page = pa.list_suppression(search="", limit=2, offset=2, admin=ADMIN, db=db)
    assert page["total"] == 3 and len(page["items"]) == 1

    with pytest.raises(HTTPException) as exc:
        pa.add_suppression(pa.EmailBody(email="not-an-address"), admin=ADMIN, db=db)
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        pa.create_removal_request(pa.RemovalBody(email="a@b.com", received_on=date.today() + timedelta(days=2)),
                                  admin=ADMIN, db=db)
    assert exc.value.status_code == 400


def test_admin_routes_are_mounted_and_admin_only():
    from api.dependencies import get_admin_user
    from api.main import app

    paths = {(r.path, tuple(sorted(r.methods))): r for r in app.routes if hasattr(r, "methods")}
    base = "/api/v1/admin/outreach"
    for path, method in ((f"{base}/suppression", "GET"), (f"{base}/suppression", "POST"),
                         (f"{base}/removal-requests", "GET"), (f"{base}/removal-requests", "POST"),
                         (f"{base}/removal-requests/{{request_id}}/delete-data", "POST")):
        route = paths[(path, (method,))]
        assert any(d.call is get_admin_user for d in route.dependant.dependencies), path


def test_pre_055_schema_falls_back_to_plaintext(monkeypatch):
    """The code can deploy before the migration is applied by hand."""
    eng = create_engine("sqlite://")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE suppressed_emails (email TEXT PRIMARY KEY, reason TEXT NOT NULL DEFAULT '',"
                       " suppressed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"))
    s = sessionmaker(bind=eng)()
    suppression.suppress(s, "A@B.com", "bounce: x")
    s.commit()
    assert suppression.is_suppressed(s, "a@b.com") and not suppression.is_suppressed(s, "c@d.com")


def test_now_is_timezone_aware_for_deadlines(db):
    from services import removal_requests as rr
    req = rr.log_request(db, "x@y.com", date(2026, 1, 31), actor="t")
    assert req.deadline - req.received_at == timedelta(days=30)
    assert req.received_at.replace(tzinfo=timezone.utc).date() == date(2026, 1, 31)
