"""Self-serve account deletion (B2C open item NEW-04).

Runs the real delete_account against a SQLite schema shaped like production.
"""
import pathlib
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import account_deletion
from services.account_deletion import UnclassifiedUserTables, delete_account, tombstone_email

DDL = [
    'CREATE TABLE "user" (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, image TEXT,'
    ' phone_number TEXT, email_verified BOOLEAN, banned BOOLEAN, ban_reason TEXT, updated_at TIMESTAMP)',
    "CREATE TABLE session (id INTEGER PRIMARY KEY, user_id TEXT)",
    "CREATE TABLE account (id INTEGER PRIMARY KEY, user_id TEXT)",
    "CREATE TABLE candidates (id INTEGER PRIMARY KEY, user_id TEXT, resume_text TEXT)",
    "CREATE TABLE leads (id INTEGER PRIMARY KEY, candidate_id INTEGER, email TEXT)",
    "CREATE TABLE email_accounts (id INTEGER PRIMARY KEY, user_id TEXT, refresh_token TEXT)",
    "CREATE TABLE payment_orders (id INTEGER PRIMARY KEY, user_id TEXT, amount_cents INTEGER)",
    "CREATE TABLE credit_ledger (id INTEGER PRIMARY KEY, user_id TEXT)",
    "CREATE TABLE tickets (id INTEGER PRIMARY KEY, user_id TEXT, user_email TEXT)",
    "CREATE TABLE coupon_issuance (code TEXT, user_id TEXT, email TEXT NOT NULL)",
    "CREATE TABLE webinar_registrations (id INTEGER PRIMARY KEY, email TEXT)",
]


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        for stmt in DDL:
            c.execute(text(stmt))
        for uid, email in (("u1", "Stu@Gmail.com"), ("u2", "other@gmail.com")):
            c.execute(text('INSERT INTO "user" (id, email, name, image, phone_number, email_verified, banned)'
                           " VALUES (:u, :e, 'Stu', 'pic', '+91999', 1, 0)"), {"u": uid, "e": email})
            c.execute(text("INSERT INTO session (user_id) VALUES (:u)"), {"u": uid})
            c.execute(text("INSERT INTO account (user_id) VALUES (:u)"), {"u": uid})
            c.execute(text("INSERT INTO candidates (user_id, resume_text) VALUES (:u, 'cv')"), {"u": uid})
            c.execute(text("INSERT INTO email_accounts (user_id, refresh_token) VALUES (:u, :t)"),
                      {"u": uid, "t": f"rt-{uid}"})
            c.execute(text("INSERT INTO payment_orders (user_id, amount_cents) VALUES (:u, 2700)"), {"u": uid})
            c.execute(text("INSERT INTO credit_ledger (user_id) VALUES (:u)"), {"u": uid})
            c.execute(text("INSERT INTO tickets (user_id, user_email) VALUES (:u, :e)"), {"u": uid, "e": email})
            c.execute(text("INSERT INTO coupon_issuance (code, user_id, email) VALUES ('X', :u, :e)"),
                      {"u": uid, "e": email})
            c.execute(text("INSERT INTO webinar_registrations (email) VALUES (:e)"), {"e": email})
        c.execute(text("INSERT INTO leads (candidate_id, email) VALUES (1, 'stu@gmail.com')"))  # a hiring manager row
    revoked = []
    monkeypatch.setattr(account_deletion, "revoke_google_grant", lambda t: revoked.append(t) or True)
    s = sessionmaker(bind=engine)()
    s.revoked = revoked
    yield s
    s.close()


def _count(db, sql, **p):
    return db.execute(text(sql), p).scalar()


def test_deletes_personal_data_and_keeps_payments(db):
    report = delete_account(db, "u1")

    assert db.revoked == ["rt-u1"] and report["revoked_gmail"] == 1
    for t in ("session", "account", "candidates", "email_accounts", "tickets"):
        assert _count(db, f"SELECT count(*) FROM {t} WHERE user_id = 'u1'") == 0, t  # noqa: S608
    assert _count(db, "SELECT count(*) FROM webinar_registrations WHERE lower(email) = 'stu@gmail.com'") == 0

    # kept: money and audit trail
    assert _count(db, "SELECT count(*) FROM payment_orders WHERE user_id = 'u1'") == 1
    assert _count(db, "SELECT count(*) FROM credit_ledger WHERE user_id = 'u1'") == 1
    assert _count(db, "SELECT email FROM coupon_issuance WHERE user_id = 'u1'") == tombstone_email("u1")

    # leads.email is the hiring manager, not the student
    assert _count(db, "SELECT count(*) FROM leads") == 1


def test_tombstone_cannot_sign_in_and_frees_the_address(db):
    delete_account(db, "u1")
    row = db.execute(text('SELECT email, name, image, phone_number, email_verified, banned FROM "user" WHERE id = \'u1\'')).one()
    assert row.email == tombstone_email("u1") and row.email.endswith(".invalid")
    assert (row.name, row.image, row.phone_number) == ("Deleted user", None, None)
    assert not row.email_verified and row.banned
    assert _count(db, "SELECT count(*) FROM \"user\" WHERE lower(email) = 'stu@gmail.com'") == 0


def test_other_users_are_untouched(db):
    delete_account(db, "u1")
    for t in ("session", "account", "candidates", "email_accounts", "tickets", "payment_orders"):
        assert _count(db, f"SELECT count(*) FROM {t} WHERE user_id = 'u2'") == 1, t  # noqa: S608
    assert _count(db, "SELECT count(*) FROM webinar_registrations WHERE email = 'other@gmail.com'") == 1
    assert _count(db, "SELECT email FROM \"user\" WHERE id = 'u2'") == "other@gmail.com"


def test_unclassified_user_table_blocks_deletion(db):
    db.execute(text("CREATE TABLE brand_new_feature (id INTEGER PRIMARY KEY, user_id TEXT)"))
    db.commit()
    with pytest.raises(UnclassifiedUserTables, match="brand_new_feature"):
        delete_account(db, "u1")
    assert db.revoked == []
    assert _count(db, "SELECT count(*) FROM session WHERE user_id = 'u1'") == 1


def test_failure_midway_rolls_everything_back(db, monkeypatch):
    """session is deleted first; the next table fails; nothing may stick."""
    db.execute(text("CREATE TABLE fails_next (id INTEGER PRIMARY KEY, user_id TEXT)"))
    db.commit()
    monkeypatch.setattr(account_deletion, "DELETE", ("session", "fails_next"))
    monkeypatch.setattr(account_deletion, "unclassified_user_tables", lambda db: [])
    orig = db.execute

    def boom(stmt, *a, **k):
        if "fails_next" in str(stmt):
            raise RuntimeError("db went away")
        return orig(stmt, *a, **k)

    monkeypatch.setattr(db, "execute", boom)
    with pytest.raises(RuntimeError):
        delete_account(db, "u1")
    monkeypatch.setattr(db, "execute", orig)
    assert _count(db, "SELECT count(*) FROM session WHERE user_id = 'u1'") == 1
    assert _count(db, "SELECT email FROM \"user\" WHERE id = 'u1'") == "Stu@Gmail.com"


def test_route_requires_typed_confirmation():
    from fastapi import HTTPException
    from api.routes_account import DeleteAccountRequest, delete_my_account

    class _U:
        id = "u1"

    with pytest.raises(HTTPException) as exc:
        delete_my_account(DeleteAccountRequest(confirm="yes"), current_user=_U(), db=None)
    assert exc.value.status_code == 400


def test_every_model_with_a_user_id_is_classified():
    """A new table with user_id must be put in KEEP or DELETE in the same PR.

    Otherwise self-serve deletion refuses everyone in production until someone
    notices (enrichment_jobs, migration 053, 29 Sep).
    """
    from database.models import Base

    known = account_deletion.KEEP | set(account_deletion.DELETE) | {"user"}
    missing = sorted(
        t.name for t in Base.metadata.tables.values()
        if "user_id" in t.columns and t.name not in known
    )
    assert missing == [], f"classify these in services/account_deletion.py: {missing}"
