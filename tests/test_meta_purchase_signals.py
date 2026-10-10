"""EX-07: the server-side Meta Purchase carries fbp, fbc, client IP and user
agent from the create-order request, not just the hashed email and user id.

Goes through the real FastAPI app: create-order stores the signals on the
PaymentOrder, and the Purchase reported after payment forwards them to
meta_capi.send_purchase.
"""
import asyncio
import pathlib
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api import routes_payment
from api.dependencies import get_current_user
from database.models import Base, CreditLedger, OutreachOrder, PaymentOrder, User, UserCredit
from database.session import get_db


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


NOW = datetime(2026, 9, 29, 12, 0, 0)
FBP = "fb.1.1727600000000.1234567890"
FBC = "fb.1.1727600000000.IwAR0abc"
UA = "Mozilla/5.0 (iPhone) Instagram 350.0"


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (User, OutreachOrder, PaymentOrder, UserCredit, CreditLedger)])
    S = sessionmaker(bind=engine)
    s = S()
    s.add(User(id="u", email="u@x.com", name="U", email_verified=True, created_at=NOW, updated_at=NOW))
    s.add(OutreachOrder(id=1, user_id="u", status="campaign_setup", action_log=[]))
    s.commit()
    s.close()

    from api.main import app

    def _db():
        d = S()
        try:
            yield d
        finally:
            d.close()
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: S().get(User, "u")

    monkeypatch.setattr(routes_payment, "_has_something_to_send", lambda db, uid: True)
    monkeypatch.setattr(routes_payment, "is_india", lambda req: True)
    monkeypatch.setattr(routes_payment, "detect_country", lambda req: "IN")
    monkeypatch.setattr(routes_payment, "capture", lambda *a, **k: None)
    fake_rz = SimpleNamespace(order=SimpleNamespace(create=lambda d: {"id": "order_rz_meta"}))
    monkeypatch.setattr(routes_payment, "_get_razorpay_client", lambda: fake_rz)
    yield TestClient(app), S
    app.dependency_overrides.clear()


def _prefix():
    from api.main import app
    for r in app.routes:
        if getattr(r, "path", "").endswith("/payment/create-order"):
            return r.path[: -len("/payment/create-order")]
    raise AssertionError("create-order route not mounted")


def test_create_order_stores_signals_and_purchase_forwards_them(env, monkeypatch):
    client, S = env
    r = client.post(
        _prefix() + "/payment/create-order",
        json={"tier": 200, "currency": "INR", "fbp": FBP, "fbc": FBC},
        headers={"User-Agent": UA, "X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
    )
    assert r.status_code == 200, r.text
    s = S()
    order = s.query(PaymentOrder).filter_by(razorpay_order_id="order_rz_meta").one()
    assert order.meta_fbp == FBP
    assert order.meta_fbc == FBC
    assert order.client_ip == "203.0.113.7"
    assert order.client_user_agent == UA

    sent = {}

    async def fake_send(**kw):
        sent.update(kw)
        return routes_payment.meta_capi.PurchaseResult("sent", http_status=200)
    monkeypatch.setattr(routes_payment.meta_capi, "is_configured", lambda: True)
    monkeypatch.setattr(routes_payment.meta_capi, "send_purchase", fake_send)
    monkeypatch.setattr(routes_payment, "_record_meta_purchase", lambda order, result: None)
    asyncio.run(routes_payment._report_purchase_to_meta(s, order))

    assert sent["event_id"] == "order_rz_meta"
    assert sent["email"] == "u@x.com"
    assert sent["fbp"] == FBP
    assert sent["fbc"] == FBC
    assert sent["client_ip"] == "203.0.113.7"
    assert sent["user_agent"] == UA


def test_malformed_or_missing_browser_ids_are_dropped(env):
    client, S = env
    r = client.post(
        _prefix() + "/payment/create-order",
        json={"tier": 200, "currency": "INR", "fbp": "<script>", "fbc": ""},
        headers={"User-Agent": UA},
    )
    assert r.status_code == 200, r.text
    order = S().query(PaymentOrder).filter_by(razorpay_order_id="order_rz_meta").one()
    assert order.meta_fbp is None
    assert order.meta_fbc is None
    # Still captured: the request itself always has them.
    assert order.client_user_agent == UA
