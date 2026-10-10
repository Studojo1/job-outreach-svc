"""Every Purchase reported to Meta leaves a meta_purchase row in system_events
(outcome, HTTP status, events_received, fbtrace_id, Meta's error), so whether
a payment was reported, and whether Meta accepted it, outlives the pod logs.

Runs _report_purchase_to_meta and meta_capi.send_purchase for real against
SQLite; only the HTTP call to Meta is faked.
"""
import asyncio
import json
import pathlib
import sys
from datetime import datetime

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api import routes_payment  # noqa: E402
from core import meta_capi  # noqa: E402
from database.models import Base, PaymentOrder, SystemEvent, User  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


NOW = datetime(2026, 10, 10, 12, 0, 0)
TOKEN = "EAAtestCapiToken0123456789"  # noqa: S105
EMAIL, IP, UA = "buyer@example.com", "203.0.113.7", "Mozilla/5.0 (iPhone) Instagram 350.0"
FBP, FBC = "fb.1.1727600000000.1234567890", "fb.1.1727600000000.IwAR0abc"
OK_BODY = {"events_received": 1, "messages": [], "fbtrace_id": "AbC123xyz"}


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


@pytest.fixture()
def meta(monkeypatch):
    """Meta configured as in production. `reply` is what the Graph API answers
    (or raises); every request made is kept in `calls`."""
    monkeypatch.setattr(meta_capi.settings, "META_CAPI_TOKEN", TOKEN)
    monkeypatch.setattr(meta_capi.settings, "META_PIXEL_ID", "123456789")
    monkeypatch.setattr(meta_capi.settings, "RAZORPAY_TEST_MODE", False)
    state = {"reply": _Resp(200, OK_BODY), "calls": []}

    class Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, params=None, json=None):
            state["calls"].append({"url": url, "params": params, "json": json})
            if isinstance(state["reply"], Exception):
                raise state["reply"]
            return state["reply"]

    monkeypatch.setattr(meta_capi.httpx, "AsyncClient", Client)
    return state


@pytest.fixture()
def S(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[User.__table__, PaymentOrder.__table__, SystemEvent.__table__])
    factory = sessionmaker(autoflush=False, bind=engine)  # as database.session.SessionLocal
    s = factory()
    s.add(User(id="u", email=EMAIL, name="B", email_verified=True, created_at=NOW, updated_at=NOW))
    s.add(PaymentOrder(id=7, user_id="u", provider="razorpay", razorpay_order_id="order_rz_7", amount_cents=149900,
                       currency="INR", tier=200, plan_id="email_200", status="paid", client_ip=IP,
                       client_user_agent=UA, meta_fbp=FBP, meta_fbc=FBC))
    s.commit()
    s.close()
    # The audit row is written through its own SessionLocal().
    monkeypatch.setattr("database.session.SessionLocal", factory)
    return factory


def _report(S, **order_changes):
    s = S()
    order = s.get(PaymentOrder, 7)
    for k, v in order_changes.items():
        setattr(order, k, v)
    s.commit()
    asyncio.run(routes_payment._report_purchase_to_meta(s, order))
    return s, order


def _rows(S):
    return [(r.event_type, r.user_id, r.meta) for r in S().query(SystemEvent).order_by(SystemEvent.created_at)]


def _no_secrets_or_personal_data(meta_row):
    blob = json.dumps(meta_row)
    for value in (TOKEN, "access_token", EMAIL, IP, UA, FBP, FBC, "graph.facebook.com"):
        assert value not in blob


def _expected(**result):
    return {"payment_order_id": 7, "event_id": "order_rz_7", "value": 1499.0, "currency": "INR",
            "owner_user_id": "u", "outcome": None, "reason": None, "http_status": None, "events_received": None,
            "fbtrace_id": None, "error": None, **result}


def test_sent_purchase_is_recorded(meta, S):
    _report(S)
    assert len(meta["calls"]) == 1 and meta["calls"][0]["json"]["data"][0]["event_id"] == "order_rz_7"
    [(event_type, user_id, row)] = _rows(S)
    # Not the buyer's own row: the frontend streams a user's system_events to them.
    assert (event_type, user_id) == ("meta_purchase", None)
    assert row == _expected(outcome="sent", http_status=200, events_received=1, fbtrace_id="AbC123xyz")
    _no_secrets_or_personal_data(row)


def test_rejected_purchase_records_metas_error(meta, S):
    meta["reply"] = _Resp(400, {"error": {
        "message": "Invalid OAuth access token - Cannot parse access token", "type": "OAuthException",
        "code": 190, "fbtrace_id": "AXyzTrace"}})
    _report(S)
    [(_, _, row)] = _rows(S)
    assert row == _expected(outcome="rejected", http_status=400, fbtrace_id="AXyzTrace",
                            error="Invalid OAuth access token - Cannot parse access token")
    _no_secrets_or_personal_data(row)


def test_long_error_is_cut_to_300_characters(meta, S):
    meta["reply"] = _Resp(400, {"error": {"message": "x" * 1000, "fbtrace_id": "T"}})
    _report(S)
    [(_, _, row)] = _rows(S)
    assert row["outcome"] == "rejected" and row["error"] == "x" * 300


def test_network_error_is_recorded_without_the_url_or_token(meta, S):
    url = f"https://graph.facebook.com/v21.0/123456789/events?access_token={TOKEN}"
    meta["reply"] = httpx.ConnectError(f"connection refused for {url}")
    _report(S)
    [(_, _, row)] = _rows(S)
    assert row == _expected(outcome="error", error="ConnectError: connection refused for [url]")
    _no_secrets_or_personal_data(row)


@pytest.mark.parametrize("changes,reason", [
    ({"meta_fbp": meta_capi.NO_CONSENT_MARK, "meta_fbc": None}, "no_consent"),
    ({"razorpay_order_id": None}, "no_event_id"),
])
def test_skips_are_recorded_and_nothing_is_sent(meta, S, changes, reason):
    _report(S, **changes)
    assert meta["calls"] == []
    [(_, user_id, row)] = _rows(S)
    assert user_id is None and row["owner_user_id"] == "u"
    assert row["outcome"] == "skipped" and row["reason"] == reason
    assert row["http_status"] is None and row["error"] is None


def test_nothing_is_recorded_when_meta_reporting_is_off(meta, S, monkeypatch):
    # Staging and test mode: no Purchase is sent, and no row is written either.
    monkeypatch.setattr(meta_capi.settings, "RAZORPAY_TEST_MODE", True)
    _report(S)
    assert meta["calls"] == [] and _rows(S) == []


def test_every_attempt_gets_its_own_row(meta, S):
    _report(S)
    _report(S)
    assert [row["outcome"] for _, _, row in _rows(S)] == ["sent", "sent"]


def test_callers_session_is_not_committed_or_rolled_back(meta, S):
    s = S()
    order = s.get(PaymentOrder, 7)
    order.plan_id = "uncommitted"
    asyncio.run(routes_payment._report_purchase_to_meta(s, order))
    assert order.plan_id == "uncommitted"  # not expired or discarded by the audit write
    s.rollback()
    assert S().get(PaymentOrder, 7).plan_id == "email_200"
    assert len(_rows(S)) == 1


def _no_table_factory():
    return sessionmaker(bind=create_engine("sqlite://"))()


def _unreachable_factory():
    raise RuntimeError("database unavailable")


@pytest.mark.parametrize("factory", [_no_table_factory, _unreachable_factory])
def test_a_failed_audit_write_does_not_break_reporting(meta, S, monkeypatch, factory):
    monkeypatch.setattr("database.session.SessionLocal", factory)
    s, order = _report(S)  # returns normally
    assert len(meta["calls"]) == 1
    assert order.status == "paid" and s.get(User, "u").email == EMAIL  # the caller's session still works
