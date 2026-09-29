# ruff: noqa: S105, S106 - fake test tokens
"""Privacy Policy v2.0: Gmail OAuth tokens are encrypted by the application.

Runs the production write paths (OAuth complete -> store_user_tokens, the
worker refresh, GmailService refresh, token_manager refresh), the backfill
and account deletion against an in-memory database, and reads the raw column
to prove what is actually stored. Only Google's HTTP endpoints are stubbed.
"""
import asyncio
import base64
import pathlib
import sys
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api import routes_gmail
from database.models import Base, EmailAccount, User
from services import gmail_tokens
from services.authentication import google_oauth, token_manager
from services.email_campaign import gmail_send_service
from services.email_campaign.gmail_service import GmailService
from services.gmail_tokens import (
    PREFIX, TokenDecryptError, backfill_encrypt_gmail_tokens, decrypt_token, encrypt_token,
)


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


NOW = datetime(2026, 9, 29, 12, 0, 0)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[User.__table__, EmailAccount.__table__])
    s = sessionmaker(bind=engine)()
    s.add(User(id="u1", email="stu@example.com", name="Stu", email_verified=True, created_at=NOW, updated_at=NOW))
    s.commit()
    yield s
    s.close()


def _raw(db, account_id):
    return db.execute(text("SELECT access_token, refresh_token FROM email_accounts WHERE id = :i"),
                      {"i": account_id}).one()


def _assert_encrypted(raw, plain):
    assert raw.startswith(PREFIX) and plain not in raw
    assert decrypt_token(raw) == plain


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.ok, self.text = status, body, status == 200, str(body)

    def json(self):
        return self._body


# ── helpers ──────────────────────────────────────────────────────────────────

def test_round_trip_and_fresh_nonce():
    a, b = encrypt_token("ya29.secret"), encrypt_token("ya29.secret")
    assert a.startswith(PREFIX) and a != b  # independent nonce per value
    assert decrypt_token(a) == decrypt_token(b) == "ya29.secret"
    assert encrypt_token(a) == a  # never double-encrypted
    assert encrypt_token(None) is None and encrypt_token("") == ""


def test_legacy_plaintext_is_readable(db):
    db.execute(text("INSERT INTO email_accounts (id, user_id, email_address, provider, access_token, refresh_token, "
                    "daily_send_limit) VALUES (1, 'u1', 'stu@gmail.com', 'gmail', 'plain-at', 'plain-rt', 10)"))
    db.commit()
    acct = db.query(EmailAccount).get(1)
    assert (acct.access_token, acct.refresh_token) == ("plain-at", "plain-rt")


def test_wrong_key_fails_loudly(monkeypatch):
    stored = encrypt_token("rt")
    from core.config import settings
    monkeypatch.setattr(settings, "LINKEDIN_ENCRYPTION_KEY", base64.b64encode(b"\x01" * 32).decode())
    with pytest.raises(TokenDecryptError):
        decrypt_token(stored)


# ── every write path stores ciphertext ───────────────────────────────────────

class _U:
    def __init__(self, uid):
        self.id = uid


def test_oauth_complete_stores_ciphertext(db, monkeypatch):
    async def exchange(code):
        return {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3599,
                "scope": "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.readonly"}

    async def info(token):
        assert token == "at-1"
        return {"email": "stu@gmail.com"}

    monkeypatch.setattr(routes_gmail, "exchange_gmail_code", exchange)
    monkeypatch.setattr(routes_gmail, "get_google_user_info", info)
    monkeypatch.setattr(routes_gmail, "capture", lambda *a, **k: None)
    out = asyncio.run(routes_gmail.gmail_oauth_complete(
        routes_gmail.GmailCompleteRequest(code="c", state=google_oauth.sign_gmail_state("u1")),
        current_user=_U("u1"), db=db))
    assert out["status"] == "connected"
    at, rt = _raw(db, out["email_account_id"])
    _assert_encrypted(at, "at-1")
    _assert_encrypted(rt, "rt-1")
    db.expire_all()
    assert db.query(EmailAccount).get(out["email_account_id"]).refresh_token == "rt-1"


def test_reconnect_over_legacy_row_stores_ciphertext(db):
    db.execute(text("INSERT INTO email_accounts (id, user_id, email_address, provider, access_token, refresh_token, "
                    "daily_send_limit) VALUES (7, 'u1', 'stu@gmail.com', 'gmail', 'old-at', 'old-rt', 10)"))
    db.commit()
    asyncio.run(token_manager.store_user_tokens(db, "u1", "stu@gmail.com", "new-at", "new-rt", 3599))
    at, rt = _raw(db, 7)
    _assert_encrypted(at, "new-at")
    _assert_encrypted(rt, "new-rt")


def _account(db, **kw):
    acct = EmailAccount(id=3, user_id="u1", email_address="stu@gmail.com", provider="gmail",
                        access_token="at-old", refresh_token="rt-old", token_expiry=NOW - timedelta(hours=1), **kw)
    db.add(acct)
    db.commit()
    return acct


def test_worker_refresh_stores_ciphertext(db, monkeypatch):
    acct = _account(db)
    sent = {}

    def post(url, data=None, timeout=None):
        sent.update(data)
        return _Resp(200, {"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 3599})

    monkeypatch.setattr(gmail_send_service.requests, "post", post)
    assert gmail_send_service._refresh_token_sync(acct, db) == "at-new"
    assert sent["refresh_token"] == "rt-old"  # Google got the plaintext, not the ciphertext
    at, rt = _raw(db, 3)
    _assert_encrypted(at, "at-new")
    _assert_encrypted(rt, "rt-new")


def test_gmail_service_refresh_stores_ciphertext(db, monkeypatch):
    acct = _account(db)
    from services.email_campaign import gmail_service
    monkeypatch.setattr(gmail_service.requests, "post",
                        lambda url, data=None, timeout=None: _Resp(200, {"access_token": "at-svc", "expires_in": 3599}))
    assert GmailService(db).refresh_token_if_needed(acct) == "at-svc"
    _assert_encrypted(_raw(db, 3)[0], "at-svc")


def test_token_manager_refresh_stores_ciphertext(db, monkeypatch):
    _account(db)

    async def refresh(rt):
        assert rt == "rt-old"
        return {"access_token": "at-tm", "expires_in": 3599}

    monkeypatch.setattr(token_manager, "refresh_gmail_access_token", refresh)
    asyncio.run(token_manager.refresh_access_token(db, "u1"))
    at, rt = _raw(db, 3)
    _assert_encrypted(at, "at-tm")
    _assert_encrypted(rt, "rt-old")


# ── backfill ─────────────────────────────────────────────────────────────────

def test_backfill_encrypts_plaintext_and_is_idempotent(db):
    db.execute(text("INSERT INTO email_accounts (id, user_id, email_address, provider, access_token, refresh_token, "
                    "daily_send_limit) VALUES (1, 'u1', 'a@gmail.com', 'gmail', 'p-at', 'p-rt', 10), "
                    "(2, 'u1', 'b@gmail.com', 'gmail', '', NULL, 10)"))
    db.add(EmailAccount(id=3, user_id="u1", email_address="c@gmail.com", access_token="c-at", refresh_token="c-rt"))
    db.commit()
    already = _raw(db, 3)

    assert backfill_encrypt_gmail_tokens(db) == 2
    at, rt = _raw(db, 1)
    _assert_encrypted(at, "p-at")
    _assert_encrypted(rt, "p-rt")
    assert tuple(_raw(db, 2)) == ("", None)  # a disconnected mailbox stays empty
    assert tuple(_raw(db, 3)) == tuple(already)  # already-encrypted row untouched

    assert backfill_encrypt_gmail_tokens(db) == 0
    assert tuple(_raw(db, 1)) == (at, rt)


def test_backfill_does_not_overwrite_a_concurrent_refresh(db, monkeypatch):
    db.execute(text("INSERT INTO email_accounts (id, user_id, email_address, provider, access_token, refresh_token, "
                    "daily_send_limit) VALUES (1, 'u1', 'a@gmail.com', 'gmail', 'stale-at', NULL, 10)"))
    db.commit()
    real = gmail_tokens.encrypt_token

    def encrypt_after_refresh(v):
        # A token refresh commits between the backfill's SELECT and its UPDATE.
        db.execute(text("UPDATE email_accounts SET access_token = :t WHERE id = 1"), {"t": real("fresh-at")})
        return real(v)

    monkeypatch.setattr(gmail_tokens, "encrypt_token", encrypt_after_refresh)
    assert backfill_encrypt_gmail_tokens(db) == 0
    assert decrypt_token(_raw(db, 1)[0]) == "fresh-at"


# ── raw-SQL reader ───────────────────────────────────────────────────────────

def test_account_deletion_revokes_with_the_decrypted_token(monkeypatch):
    from services import account_deletion
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(text('CREATE TABLE "user" (id TEXT PRIMARY KEY, email TEXT, name TEXT, image TEXT, phone_number '
                       'TEXT, email_verified BOOLEAN, banned BOOLEAN, ban_reason TEXT, updated_at TIMESTAMP)'))
        c.execute(text("CREATE TABLE email_accounts (id INTEGER PRIMARY KEY, user_id TEXT, refresh_token TEXT)"))
        c.execute(text("INSERT INTO \"user\" (id, email, name) VALUES ('u1', 's@x.com', 'S')"))
        c.execute(text("INSERT INTO email_accounts (user_id, refresh_token) VALUES ('u1', :t), ('u1', 'legacy-rt')"),
                  {"t": encrypt_token("enc-rt")})
    revoked = []
    monkeypatch.setattr(account_deletion, "revoke_google_grant", lambda t: revoked.append(t) or True)
    account_deletion.delete_account(sessionmaker(bind=engine)(), "u1")
    assert sorted(revoked) == ["enc-rt", "legacy-rt"]
