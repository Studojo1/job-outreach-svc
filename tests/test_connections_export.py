# ruff: noqa: S105, S106 - fake test tokens
"""Privacy Policy v2.0: disconnect Gmail / LinkedIn from Settings, download my data.

Runs the production routes in api/routes_connections.py (and the services
behind them) against an in-memory database. Only Google's revoke call is stubbed.
"""
import json
import pathlib
import sys
from datetime import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api import routes_connections
from database.models import (
    Base, Campaign, Candidate, CreditLedger, EmailAccount, EmailSent, Lead, LinkedInCampaign,
    LinkedInConnectionRequest, LinkedInToken, OutreachOrder, PaymentOrder, SystemEvent, User, UserCredit,
)
from services import account_deletion
from services.gmail_tokens import PREFIX


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


NOW = datetime(2026, 9, 29, 12, 0, 0)


class _U:
    def __init__(self, uid):
        self.id = uid


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (
        User, Candidate, Lead, EmailAccount, Campaign, EmailSent, SystemEvent, OutreachOrder, PaymentOrder,
        UserCredit, CreditLedger, LinkedInToken, LinkedInCampaign, LinkedInConnectionRequest)])
    with engine.begin() as c:  # owned by another service in production
        c.execute(text("CREATE TABLE user_linkedin_sessions (id TEXT, user_id TEXT, li_at_encrypted TEXT, "
                       "cookies_encrypted TEXT, is_active BOOLEAN)"))
        c.execute(text("INSERT INTO user_linkedin_sessions VALUES ('s1', 'u1', 'x', 'y', 1), ('s2', 'u2', 'x', 'y', 1)"))
    s = sessionmaker(bind=engine)()
    for uid in ("u1", "u2"):
        s.add(User(id=uid, email=f"{uid}@example.com", name=uid, email_verified=True, created_at=NOW, updated_at=NOW))
    s.add_all([
        Candidate(id=1, user_id="u1", resume_text="My resume", quiz_answers={"role": "PM"}),
        Candidate(id=2, user_id="u2", resume_text="Other resume"),
        Lead(id=1, candidate_id=1, name="Hiring Manager", email="hm@acme.com", company="Acme"),
        EmailAccount(id=10, user_id="u1", email_address="u1@gmail.com", provider="gmail",
                     access_token="at-u1", refresh_token="rt-u1", created_at=NOW),
        EmailAccount(id=20, user_id="u2", email_address="u2@gmail.com", provider="gmail",
                     access_token="at-u2", refresh_token="rt-u2", created_at=NOW),
        Campaign(id=1, candidate_id=1, email_account_id=10, name="Run", status="running"),
        Campaign(id=2, candidate_id=1, email_account_id=10, name="Done", status="completed"),
        Campaign(id=3, candidate_id=2, email_account_id=20, name="Theirs", status="running"),
        EmailSent(id=1, campaign_id=1, lead_id=1, to_email="hm@acme.com", subject="Hi", body="Hello",
                  status="sent", sent_at=NOW, reply_text="Let's talk", tracking_token="trk-secret",
                  message_id="gmail-msg-id"),
        PaymentOrder(id=1, user_id="u1", provider="razorpay", razorpay_order_id="order_1",
                     razorpay_payment_id="pay_1", razorpay_signature="sig-secret", idempotency_key="idem-secret",
                     amount_cents=2700, currency="INR", tier=50, status="paid", credits_granted=50),
        UserCredit(user_id="u1", total_credits=50, used_credits=10),
        CreditLedger(user_id="u1", delta_total=50, reason="purchase", actor="admin-secret-id", payment_order_id=1),
        LinkedInToken(user_id="u1", li_at_enc="li-at-ciphertext", jsessionid_enc="js-ciphertext", nonce="nonce",
                      connection_mode="extension", cookies_blob_enc="blob-ciphertext", cookies_blob_nonce="n2"),
        LinkedInToken(user_id="u2", li_at_enc="c", jsessionid_enc="c", nonce="n", connection_mode="proxy"),
        LinkedInCampaign(id=1, user_id="u1", name="LI", status="running", target_role="PM"),
        LinkedInCampaign(id=2, user_id="u2", name="LI2", status="running", target_role="PM"),
        LinkedInConnectionRequest(id=1, campaign_id=1, user_id="u1", name="Lead", profile_url="https://li/x",
                                  status="replied", reply_text="Thanks"),
    ])
    s.commit()
    revoked = []
    monkeypatch.setattr(account_deletion, "revoke_google_grant", lambda t: revoked.append(t) or True)
    s.revoked = revoked
    yield s
    s.close()


def _connections(db, uid="u1"):
    return routes_connections.get_connections(current_user=_U(uid), db=db)


def test_connections_status(db):
    assert _connections(db) == {"gmail": {"connected": True, "email": "u1@gmail.com"},
                                "linkedin": {"connected": True, "method": "extension"}}
    assert _connections(db, "u2")["linkedin"]["method"] == "password"


def test_gmail_disconnect_revokes_deletes_tokens_and_pauses(db):
    out = routes_connections.gmail_disconnect(current_user=_U("u1"), db=db)
    assert out == {"disconnected": True, "campaigns_paused": 1}
    assert db.revoked == ["rt-u1"]  # the plaintext refresh token went to Google

    raw = db.execute(text("SELECT access_token, refresh_token, token_expiry FROM email_accounts WHERE id = 10")).one()
    assert tuple(raw) == ("", None, None)
    run, done = db.get(Campaign, 1), db.get(Campaign, 2)
    assert (run.status, run.pause_reason, run.paused_by) == ("paused", "gmail_disconnected", "user")
    assert run.paused_at is not None and done.status == "completed"
    ev = db.query(SystemEvent).filter_by(event_type="campaign_paused").one()
    assert ev.meta["pause_reason"] == "gmail_disconnected" and ev.meta["owner_user_id"] == "u1"
    # campaigns and sent history survive (the row is not deleted: it would cascade)
    assert db.query(EmailSent).count() == 1

    # the other user is untouched
    assert db.get(Campaign, 3).status == "running"
    assert db.execute(text("SELECT refresh_token FROM email_accounts WHERE id = 20")).scalar().startswith(PREFIX)

    assert _connections(db)["gmail"] == {"connected": False, "email": None}

    # idempotent
    again = routes_connections.gmail_disconnect(current_user=_U("u1"), db=db)
    assert again == {"disconnected": True, "campaigns_paused": 0}
    assert db.revoked == ["rt-u1"]


def test_gmail_disconnect_then_reconnect_reuses_the_row(db):
    import asyncio

    from services.authentication.token_manager import store_user_tokens
    routes_connections.gmail_disconnect(current_user=_U("u1"), db=db)
    asyncio.run(store_user_tokens(db, "u1", "u1@gmail.com", "at-2", "rt-2", 3599))
    assert db.query(EmailAccount).filter_by(user_id="u1").count() == 1
    assert _connections(db)["gmail"] == {"connected": True, "email": "u1@gmail.com"}


def test_gmail_disconnect_still_deletes_tokens_when_revoke_fails(db, monkeypatch):
    monkeypatch.setattr(account_deletion, "revoke_google_grant", lambda t: False)
    routes_connections.gmail_disconnect(current_user=_U("u1"), db=db)
    assert db.execute(text("SELECT refresh_token FROM email_accounts WHERE id = 10")).scalar() is None


def test_linkedin_disconnect_clears_credentials_and_pauses(db):
    assert routes_connections.linkedin_disconnect(current_user=_U("u1"), db=db) == {"disconnected": True}
    assert db.query(LinkedInToken).filter_by(user_id="u1").count() == 0
    assert db.execute(text("SELECT count(*) FROM user_linkedin_sessions WHERE user_id = 'u1'")).scalar() == 0
    assert db.get(LinkedInCampaign, 1).status == "paused"
    assert _connections(db)["linkedin"] == {"connected": False, "method": None}
    # other user untouched
    assert db.query(LinkedInToken).filter_by(user_id="u2").count() == 1
    assert db.execute(text("SELECT count(*) FROM user_linkedin_sessions WHERE user_id = 'u2'")).scalar() == 1
    assert db.get(LinkedInCampaign, 2).status == "running"
    # idempotent
    assert routes_connections.linkedin_disconnect(current_user=_U("u1"), db=db) == {"disconnected": True}


def test_export_has_the_users_data_and_no_secrets(db):
    resp = routes_connections.export_my_data(current_user=_U("u1"), db=db)
    assert resp.headers["content-disposition"].startswith('attachment; filename="studojo-data-')
    assert resp.headers["content-disposition"].endswith('.json"')
    body = resp.body.decode()
    data = json.loads(body)

    assert data["profile"]["email"] == "u1@example.com"
    assert data["candidates"][0]["resume_text"] == "My resume"
    assert data["candidates"][0]["quiz_answers"] == {"role": "PM"}
    assert {c["name"] for c in data["campaigns"]} == {"Run", "Done"}
    e = data["emails_sent"][0]
    assert (e["to_email"], e["subject"], e["body"], e["status"], e["reply_text"]) == (
        "hm@acme.com", "Hi", "Hello", "sent", "Let's talk")
    assert data["payments"][0]["amount_cents"] == 2700
    assert data["credits"]["total_credits"] == 50 and data["credits"]["ledger"][0]["reason"] == "purchase"
    assert data["linkedin_campaigns"][0]["name"] == "LI"
    assert data["linkedin_connection_requests"][0]["reply_text"] == "Thanks"
    assert data["gmail_accounts"][0]["email_address"] == "u1@gmail.com"

    for secret in ("at-u1", "rt-u1", PREFIX, "sig-secret", "idem-secret", "trk-secret", "li-at-ciphertext",
                   "js-ciphertext", "blob-ciphertext", "admin-secret-id", "Other resume", "u2@gmail.com"):
        assert secret not in body, secret
