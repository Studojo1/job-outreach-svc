"""The payment confirmation handlers run their DB work off the event loop
(audit P15), and still work end to end through the real FastAPI app."""
import hashlib
import hmac
import json
import pathlib
import sys
from datetime import datetime

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


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (User, OutreachOrder, PaymentOrder, UserCredit, CreditLedger)])
    S = sessionmaker(bind=engine)
    s = S()
    s.add(User(id="u", email="u@x.com", name="U", email_verified=True, created_at=NOW, updated_at=NOW))
    s.add(OutreachOrder(id=1, user_id="u", status="campaign_setup", action_log=[]))
    s.add(PaymentOrder(id=7, user_id="u", status="created", provider="dodo", amount_cents=2700, currency="USD",
                       tier=200, plan_id="email_200", dodo_checkout_id="cs_1", outreach_order_id=1, created_at=NOW))
    s.add(PaymentOrder(id=8, user_id="u", status="created", provider="razorpay", amount_cents=182500, currency="INR",
                       tier=200, plan_id="email_200", razorpay_order_id="order_rz", outreach_order_id=1, created_at=NOW))
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
    app.dependency_overrides[get_current_user] = lambda: s.get(User, "u") or S().get(User, "u")

    async def meta(*a, **k):
        return None
    monkeypatch.setattr(routes_payment, "_report_purchase_to_meta", meta)
    monkeypatch.setattr(routes_payment, "capture", lambda *a, **k: None)
    yield TestClient(app), S
    app.dependency_overrides.clear()


def _prefix():
    from api.main import app
    for r in app.routes:
        if getattr(r, "path", "").endswith("/payment/verify-dodo"):
            return r.path[: -len("/payment/verify-dodo")]
    raise AssertionError("verify-dodo route not mounted")


def test_dodo_poll_grants_once_through_the_real_app(env, monkeypatch):
    client, S = env
    async def paid(session_id):
        return {"status": "succeeded", "payment_id": "pay_d"}
    monkeypatch.setattr(routes_payment.dodo_svc, "get_checkout_status", paid)
    url = _prefix() + "/payment/verify-dodo"
    r1 = client.post(url, json={"session_id": "cs_1"})
    r2 = client.post(url, json={"session_id": "cs_1"})
    assert r1.status_code == 200 and r1.json()["status"] == "paid"
    assert r2.json()["status"] == "paid"
    s = S()
    assert s.query(UserCredit).filter_by(user_id="u").one().total_credits == 200  # granted once


def test_signed_razorpay_webhook_marks_paid_through_the_real_app(env, monkeypatch):
    client, S = env
    monkeypatch.setattr(routes_payment.settings, "RAZORPAY_WEBHOOK_SECRET", "whsec")
    body = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {
        "order_id": "order_rz", "id": "pay_rz"}}}}).encode()
    sig = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    r = client.post(_prefix() + "/payment/webhook", content=body,
                    headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    s = S()
    assert s.get(PaymentOrder, 8).status == "paid"
