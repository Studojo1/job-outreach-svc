"""B2C audit 30 Sep 2026, payments stream.

PP-P14 (coupon use counts), PP-P05 (refunds recorded, provider refund
webhooks), UC-Q05 (webhook signature alarm, Dodo webhook fails closed),
ST-N07 (no Meta CAPI from test mode), OP-N10 (orders with leads are
leads_ready), CF-N05 (schema check, no SQL in 5xx), NEW-09 (retired plans not
sold), ST-N04 (reconciler path is tagged).

Every test calls production code.
"""
import asyncio
import hashlib
import hmac
import json
import pathlib
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import (  # noqa: E402
    Base, Campaign, Candidate, Coupon, CreditLedger, EmailAccount, EmailSent, OutreachOrder,
    PaymentOrder, PaymentRefund, SystemEvent, User, UserCredit,
)


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


NOW = datetime(2026, 9, 30, 12, 0, 0)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (
        User, Candidate, Campaign, EmailAccount, EmailSent, OutreachOrder, Coupon,
        PaymentOrder, PaymentRefund, UserCredit, CreditLedger, SystemEvent)])
    s = sessionmaker(bind=engine)()
    s.add_all([
        User(id="u", email="u@x.com", name="U", email_verified=True, created_at=NOW, updated_at=NOW),
        User(id="v", email="v@x.com", name="V", email_verified=True, created_at=NOW, updated_at=NOW),
        Candidate(id=1, user_id="u", resume_text="."),
        UserCredit(user_id="u", total_credits=200, used_credits=0),
        Campaign(id=10, candidate_id=1, name="a", status="running", daily_limit=20,
                 credits_reserved=0, credits_released=0),
    ])
    s.commit()
    yield s
    s.close()


def _paid(db, oid=50, provider="razorpay", amount=182500, credits_granted=200):
    db.add(PaymentOrder(id=oid, user_id="u", status="paid", provider=provider, amount_cents=amount,
                        currency="INR", tier=200, credits_granted=credits_granted,
                        razorpay_payment_id=f"pay_{oid}", dodo_payment_id=f"pd_{oid}", created_at=NOW,
                        updated_at=NOW))
    db.commit()


@pytest.fixture(autouse=True)
def _no_founder_email(monkeypatch):
    from services import reconcile
    monkeypatch.setattr(reconcile, "_tell_founders", lambda *a, **k: None)


# ── PP-P14: coupon use counts ───────────────────────────────────────────────

def _coupon(db, cid=10, max_uses=32, uses=0):
    db.add(Coupon(id=cid, code=f"C{cid}", discount_type="percent", discount_value=100,
                  max_uses=max_uses, uses=uses, is_active=True))
    db.commit()


def _coupon_order(db, oid, status, user="u", coupon_id=10, created_at=NOW):
    db.add(PaymentOrder(id=oid, user_id=user, status=status, provider="razorpay", amount_cents=100,
                        currency="INR", tier=200, coupon_id=coupon_id, created_at=created_at))
    db.commit()


def test_coupon_uses_come_from_payments_and_repeat_confirmations_do_not_double_count(db):
    from api.routes_payment import sync_coupon_uses
    _coupon(db)
    _coupon_order(db, 1, "paid")
    _coupon_order(db, 2, "created")
    for _ in range(3):  # verify, webhook and reconciler all confirming one payment
        sync_coupon_uses(db, 10)
        db.commit()
    assert db.get(Coupon, 10).uses == 1


def test_coupon_uses_are_never_lowered(db):
    from api.routes_payment import sync_coupon_uses
    _coupon(db, uses=32)  # an admin closed the code by hand
    _coupon_order(db, 1, "paid")
    sync_coupon_uses(db, 10)
    db.commit()
    assert db.get(Coupon, 10).uses == 32


def test_other_buyers_open_checkouts_hold_the_last_use_but_my_own_retry_does_not(db):
    from api.routes_payment import coupon_exhausted
    _coupon(db, max_uses=2)
    _coupon_order(db, 1, "paid", user="v")
    _coupon_order(db, 2, "created", user="v", created_at=datetime.utcnow())
    coupon = db.get(Coupon, 10)
    assert coupon_exhausted(db, coupon, "u")          # v's checkout holds the last use
    assert not coupon_exhausted(db, coupon, "v")      # v retrying is not blocked by v
    old = db.get(PaymentOrder, 2)
    old.created_at = datetime.utcnow() - timedelta(hours=2)  # abandoned
    db.commit()
    assert not coupon_exhausted(db, coupon, "u")


def test_hourly_reconcile_repairs_a_drifted_counter(db):
    from services import reconcile
    _coupon(db, uses=0)
    _coupon_order(db, 1, "paid")
    _coupon_order(db, 2, "refunded", user="v")
    assert reconcile.sync_coupon_counts(db, NOW) == 1
    assert db.get(Coupon, 10).uses == 2


# ── PP-P05: refunds recorded once, provider refunds written back ────────────

def test_admin_refund_is_recorded_and_its_webhook_echo_is_a_no_op(db, monkeypatch):
    from services import refunds

    async def ok(order, reason, amount_cents=None):
        return "rfnd_1"
    monkeypatch.setattr(refunds, "_provider_refund", ok)
    _paid(db)
    asyncio.run(refunds.refund_payment(db, 50, actor="admin", reason="never received service"))
    row = db.query(PaymentRefund).one()
    assert (row.provider_refund_id, row.amount_cents, row.source, row.credits_revoked) == ("rfnd_1", 182500, "admin", 200)
    assert refunds.apply_provider_refund(db, provider="razorpay", payment_id="pay_50", refund_id="rfnd_1",
                                         amount_cents=182500) == "duplicate"
    assert db.query(UserCredit).one().total_credits == 0
    assert db.query(PaymentRefund).count() == 1


def test_refund_clicked_in_the_dashboard_is_settled_in_the_app(db):
    from services import refunds
    _paid(db, provider="dodo")
    assert refunds.apply_provider_refund(db, provider="dodo", payment_id="pd_50", refund_id="rf_dash",
                                         amount_cents=182500, currency="INR") == "settled"
    order = db.get(PaymentOrder, 50)
    assert (order.status, order.refunded_cents) == ("refunded", 182500)
    assert db.get(Campaign, 10).status == "cancelled"
    assert db.query(UserCredit).one().total_credits == 0
    assert db.query(PaymentRefund).one().source == "webhook"


def test_partial_dashboard_refund_revokes_the_same_share_of_credits(db):
    from services import refunds
    _paid(db, amount=200000)
    refunds.apply_provider_refund(db, provider="razorpay", payment_id="pay_50", refund_id="rf_half",
                                  amount_cents=100000)
    order = db.get(PaymentOrder, 50)
    assert (order.status, order.refunded_cents) == ("paid", 100000)
    assert db.query(UserCredit).one().total_credits == 100
    assert db.get(Campaign, 10).status == "running"


def test_webhook_waits_while_our_own_refund_is_mid_way(db):
    from services import refunds
    _paid(db)
    order = db.get(PaymentOrder, 50)
    order.status, order.updated_at = "refunding", datetime.utcnow()
    db.commit()
    with pytest.raises(refunds.RefundInFlight):
        refunds.apply_provider_refund(db, provider="razorpay", payment_id="pay_50", refund_id="rfnd_x",
                                      amount_cents=182500)


def test_a_second_click_cannot_refund_twice(db, monkeypatch):
    from services import refunds
    _paid(db)
    order = db.get(PaymentOrder, 50)
    order.status, order.updated_at = "refunding", datetime.utcnow()
    db.commit()

    async def never(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("provider called twice")
    monkeypatch.setattr(refunds, "_provider_refund", never)
    with pytest.raises(refunds.RefundError, match="in progress"):
        asyncio.run(refunds.refund_payment(db, 50, actor="admin", reason="x"))


def test_provider_refusal_releases_the_claim(db, monkeypatch):
    from services import refunds
    _paid(db)

    async def boom(order, reason, amount_cents=None):
        raise RuntimeError("provider said no")
    monkeypatch.setattr(refunds, "_provider_refund", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(refunds.refund_payment(db, 50, actor="admin", reason="x"))
    assert db.get(PaymentOrder, 50).status == "paid"
    assert db.query(PaymentRefund).count() == 0


def test_razorpay_refund_webhook_reaches_the_settlement(db):
    from api.routes_payment import _razorpay_webhook_apply
    _paid(db)
    body = json.dumps({"event": "refund.processed", "payload": {"refund": {"entity": {
        "id": "rfnd_dash", "payment_id": "pay_50", "amount": 182500, "currency": "INR"}}}}).encode()
    _razorpay_webhook_apply(body, db)
    assert db.get(PaymentOrder, 50).status == "refunded"


def test_dodo_refund_webhook_reaches_the_settlement(db):
    from api.routes_payment import _dodo_webhook_apply
    _paid(db, provider="dodo")
    body = json.dumps({"type": "refund.succeeded", "data": {
        "refund_id": "rf_1", "payment_id": "pd_50", "amount": 182500, "currency": "INR"}}).encode()
    _dodo_webhook_apply(body, db)
    assert db.get(PaymentOrder, 50).status == "refunded"


# ── UC-Q05: webhooks fail closed and a dead secret raises the alarm ─────────

class _Req:
    def __init__(self, body, headers):
        self._b, self.headers = body, headers

    async def body(self):
        return self._b


def test_dodo_webhook_without_a_secret_refuses_everything(monkeypatch):
    from api import routes_payment
    monkeypatch.setattr(routes_payment.settings, "DODO_WEBHOOK_SECRET", "")
    with pytest.raises(HTTPException) as e:
        asyncio.run(routes_payment.dodo_webhook(_Req(b"{}", {}), db=None))
    assert e.value.status_code == 503


def test_every_webhook_failing_its_signature_raises_the_alarm(monkeypatch):
    from api import routes_payment
    from services import webhook_health
    webhook_health.reset()
    alerts = []
    monkeypatch.setattr(webhook_health, "_alert_founders", lambda *a: alerts.append(a))
    monkeypatch.setattr(routes_payment.settings, "RAZORPAY_WEBHOOK_SECRET", "right")
    for _ in range(webhook_health.FAIL_THRESHOLD):
        with pytest.raises(HTTPException):
            asyncio.run(routes_payment.razorpay_webhook(_Req(b"{}", {"X-Razorpay-Signature": "bad"}), db=None))
    import time
    time.sleep(0.05)  # the alert runs on its own thread
    assert alerts and alerts[0][0] == "razorpay"
    webhook_health.reset()


def test_a_good_delivery_in_the_window_means_no_alarm():
    from services import webhook_health
    webhook_health.reset()
    webhook_health.record("razorpay", ok=True, now=NOW)
    fired = [webhook_health.record("razorpay", ok=False, now=NOW + timedelta(minutes=i)) for i in range(5)]
    assert not any(fired)
    webhook_health.reset()


def test_signed_razorpay_webhook_still_accepted(monkeypatch):
    from api import routes_payment
    from services import webhook_health
    webhook_health.reset()
    monkeypatch.setattr(routes_payment.settings, "RAZORPAY_WEBHOOK_SECRET", "right")
    body = b'{"event": "order.paid"}'
    sig = hmac.new(b"right", body, hashlib.sha256).hexdigest()
    out = asyncio.run(routes_payment.razorpay_webhook(_Req(body, {"X-Razorpay-Signature": sig}), db=None))
    assert out == {"status": "ok"}
    webhook_health.reset()


# ── ST-N07: test mode never reports to the live Meta dataset ────────────────

def test_meta_capi_is_off_in_test_mode(monkeypatch):
    from core import meta_capi
    monkeypatch.setattr(meta_capi.settings, "META_CAPI_TOKEN", "tok")
    monkeypatch.setattr(meta_capi.settings, "RAZORPAY_TEST_MODE", True)
    assert not meta_capi.is_configured()
    monkeypatch.setattr(meta_capi.settings, "RAZORPAY_TEST_MODE", False)
    assert meta_capi.is_configured()


# ── OP-N10: an unpaid order with leads is leads_ready ───────────────────────

def test_orders_holding_leads_leave_created(db):
    from services import reconcile
    db.add_all([
        OutreachOrder(id=1, user_id="u", status="created", leads_generated_at=NOW),
        OutreachOrder(id=2, user_id="u", status="profile_complete", leads_generated_at=NOW),
        OutreachOrder(id=3, user_id="u", status="created"),                                      # no leads yet
        OutreachOrder(id=4, user_id="u", status="created", leads_generated_at=NOW, payment_made_at=NOW),  # paid
    ])
    db.commit()
    assert reconcile.advance_orders_with_leads(db, NOW) == 2
    assert [db.get(OutreachOrder, i).status for i in (1, 2, 3, 4)] == ["leads_ready", "leads_ready", "created", "created"]


def test_migration_070_backfills_the_same_rows():
    sql = (pathlib.Path(__file__).resolve().parents[1] / "migrations" / "070_payment_refunds_and_order_status.sql").read_text()
    assert "SET status = 'leads_ready'" in sql and "payment_made_at IS NULL" in sql
    assert "CREATE TABLE IF NOT EXISTS payment_refunds" in sql


# ── CF-N05: the pod refuses to run ahead of its migration; no SQL in 5xx ───

def test_schema_check_names_the_missing_column():
    from core.deploy_guards import SchemaMismatch, check_schema, missing_schema
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
    md = MetaData()
    Table("t", md, Column("id", Integer, primary_key=True), Column("psychometric_profile", Integer))
    assert missing_schema(engine, md) == ["column t.psychometric_profile"]
    with pytest.raises(SchemaMismatch):
        check_schema(engine, md)


def test_schema_check_flags_payment_refunds_until_migration_070_runs():
    from core.deploy_guards import missing_schema
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[t.__table__
                                             for t in (User, Coupon, OutreachOrder, PaymentOrder)])
    missing = missing_schema(engine, Base.metadata)
    assert "table payment_refunds" in missing
    assert not [m for m in missing if m.startswith("column payment_orders.")]
    Base.metadata.create_all(engine, tables=[PaymentRefund.__table__])
    assert "table payment_refunds" not in missing_schema(engine, Base.metadata)


def test_a_5xx_carrying_sql_is_replaced_with_safe_copy():
    from core.deploy_guards import SAFE_5XX_DETAIL, safe_http_exception_handler
    req = SimpleNamespace(method="POST", url=SimpleNamespace(path="/x"), headers={})
    leak = ('(psycopg2.errors.UndefinedColumn) column candidates.psychometric_profile does not exist '
            '[SQL: SELECT candidates.id FROM candidates] (Background on this error at: https://sqlalche.me/e/20/f405)')
    res = asyncio.run(safe_http_exception_handler(req, HTTPException(status_code=500, detail=leak)))
    assert json.loads(res.body) == {"detail": SAFE_5XX_DETAIL}
    ok = asyncio.run(safe_http_exception_handler(req, HTTPException(status_code=502, detail="Payment gateway error.")))
    assert json.loads(ok.body) == {"detail": "Payment gateway error."}


# ── NEW-09: LinkedIn and combined plans cannot be bought ────────────────────

@pytest.mark.parametrize("plan_id", ["linkedin_weekly", "linkedin_monthly", "both_200", "both_350", "both_500"])
def test_retired_plans_cannot_be_bought(plan_id):
    from api.routes_payment import CreateOrderRequest, create_order
    with pytest.raises(HTTPException) as e:
        asyncio.run(create_order(CreateOrderRequest(plan_id=plan_id), req=SimpleNamespace(headers={}),
                                 current_user=SimpleNamespace(id="u"), db=None))
    assert e.value.status_code == 400 and "no longer available" in e.value.detail
