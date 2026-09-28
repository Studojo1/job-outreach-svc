"""Post-payment audit, wave 0: ownership, internal auth, and create-refund guards.

- P07: /campaign/worker/send-ready and /mesa/worker/run-due were public via the
  studojo.com ingress. They now need the shared x-studojo-internal secret.
- P28/P40: transition, send, GET and metrics took any campaign id.
- Test sends and campaign create took any email_account_id, i.e. someone
  else's Gmail.
- P32: a failed create refunded `lead_limit or 200` even when nothing had been
  reserved yet.
"""
import pathlib
import sys

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api import dependencies
from api.routes_campaign import (
    _owned_campaign,
    _owned_email_account,
    _release_create_reservation,
)
from database.models import Base, Campaign, Candidate, CreditLedger, EmailAccount, UserCredit


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine, tables=[t.__table__ for t in (Candidate, Campaign, EmailAccount, UserCredit, CreditLedger)]
    )
    session = sessionmaker(bind=engine)()
    session.add_all([
        Candidate(id=1, user_id="me", resume_text="."),
        Candidate(id=2, user_id="them", resume_text="."),
        Campaign(id=10, candidate_id=1, name="mine"),
        Campaign(id=20, candidate_id=2, name="theirs"),
        EmailAccount(id=100, user_id="me", email_address="me@x", access_token="t"),  # noqa: S106
        EmailAccount(id=200, user_id="them", email_address="them@x", access_token="t"),  # noqa: S106
        UserCredit(user_id="me", total_credits=200, used_credits=150),
    ])
    session.commit()
    yield session
    session.close()


def _request(headers: dict) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw})


# ── internal worker routes ──────────────────────────────────────────────────

def test_worker_route_rejects_callers_without_the_secret(monkeypatch):
    monkeypatch.setattr(dependencies.settings, "INTERNAL_API_SECRET", "s3cret")
    for headers in ({}, {"x-studojo-internal": "guess"}):
        with pytest.raises(HTTPException) as exc:
            dependencies.require_internal_caller(_request(headers))
        assert exc.value.status_code == 401


def test_worker_route_rejects_everyone_when_no_secret_is_configured(monkeypatch):
    monkeypatch.setattr(dependencies.settings, "INTERNAL_API_SECRET", "")
    with pytest.raises(HTTPException):
        dependencies.require_internal_caller(_request({"x-studojo-internal": ""}))


def test_worker_route_accepts_the_secret(monkeypatch):
    monkeypatch.setattr(dependencies.settings, "INTERNAL_API_SECRET", "s3cret")
    dependencies.require_internal_caller(_request({"x-studojo-internal": "s3cret"}))


def test_worker_routes_are_wired_to_the_guard():
    from api.routes_campaign import router as campaign_router
    from api.routes_mesa import router as mesa_router
    for router, path in ((campaign_router, "/campaign/worker/send-ready"),
                         (mesa_router, "/mesa/worker/run-due")):
        route = next(r for r in router.routes if getattr(r, "path", "") == path)
        calls = [d.call for d in route.dependant.dependencies]
        assert dependencies.require_internal_caller in calls, path


# ── ownership ───────────────────────────────────────────────────────────────

def test_own_campaign_is_returned(db):
    assert _owned_campaign(db, 10, "me").id == 10


def test_someone_elses_campaign_is_403(db):
    with pytest.raises(HTTPException) as exc:
        _owned_campaign(db, 20, "me")
    assert exc.value.status_code == 403


def test_missing_campaign_is_404(db):
    with pytest.raises(HTTPException) as exc:
        _owned_campaign(db, 999, "me")
    assert exc.value.status_code == 404


def test_someone_elses_gmail_is_not_usable(db):
    assert _owned_email_account(db, 100, "me").id == 100
    assert _owned_email_account(db, 200, "me") is None


# ── create-failure refund ───────────────────────────────────────────────────

def _used(db):
    return db.query(UserCredit).filter_by(user_id="me").one().used_credits


def test_failure_before_reservation_refunds_nothing(db):
    _release_create_reservation(db, "me", 0)
    assert _used(db) == 150


def test_failure_after_reservation_refunds_exactly_what_was_reserved(db):
    _release_create_reservation(db, "me", 50)
    assert _used(db) == 100


def test_refund_never_goes_negative(db):
    _release_create_reservation(db, "me", 500)
    assert _used(db) == 0
