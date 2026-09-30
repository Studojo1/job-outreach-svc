"""Refund Policy v3.0 §3.3: the campaign refund check and the partial refund.

Runs the production services/refund_check.py and services/refunds.py against
an in-memory database. Only the provider call is stubbed.
"""
import asyncio
import pathlib
import sys
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import (
    Base, Campaign, CampaignNotice, Candidate, Coupon, CreditLedger, EmailSent, OutreachOrder,
    PaymentOrder, PaymentRefund, SystemEvent, User, UserCredit,
)
from services import refunds
from services.refund_check import campaign_refund_check


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


START = datetime(2026, 9, 10, 9, 0, 0)
NOW = datetime(2026, 10, 6, 12, 0, 0)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (
        User, Candidate, Campaign, EmailSent, CampaignNotice, SystemEvent, Coupon, OutreachOrder,
        PaymentOrder, PaymentRefund, UserCredit, CreditLedger)])
    s = sessionmaker(bind=engine)()
    s.add_all([
        User(id="u", email="aarav@example.com", name="Aarav", email_verified=True, created_at=START, updated_at=START),
        Candidate(id=1, user_id="u", resume_text="."),
        OutreachOrder(id=5, user_id="u", candidate_id=1),
        # Growth pack: 200 credits for Rs 1,825
        PaymentOrder(id=9, user_id="u", provider="razorpay", razorpay_order_id="order_x",
                     razorpay_payment_id="pay_x", amount_cents=182500, currency="INR", tier=200,
                     credits_granted=200, outreach_order_id=5, status="paid", created_at=START),
        UserCredit(user_id="u", total_credits=200, used_credits=200),
        Campaign(id=1, candidate_id=1, name="Product internships", status="running", user_timezone="UTC",
                 created_at=START, started_at=START, credits_reserved=200, outreach_order_id=5),
    ])
    # 98 first-touch sends over 10-18 Sep, then nothing.
    per_day = [12, 11, 11, 11, 11, 11, 11, 10, 10]
    for i, n in enumerate(per_day):
        for _ in range(n):
            s.add(EmailSent(campaign_id=1, status="sent", followup_number=0,
                            sent_at=START + timedelta(days=i, hours=1)))
    s.commit()
    yield s
    s.close()


def _cond(result, n):
    return next(c for c in result["conditions"] if c["n"] == n)["result"]


def test_dead_campaign_reported_and_not_fixed_meets_33(db):
    r = campaign_refund_check(db, 1, reported_on=date(2026, 9, 28), now=NOW)
    assert r["campaign"]["first_touch_sent"] == 98
    assert r["streak"] == {"days": 18, "start": "2026-09-19", "end": "2026-10-06"}
    assert [_cond(r, n) for n in (1, 2, 3, 5, 6)] == ["yes"] * 5
    assert _cond(r, 4) == "check"
    assert r["verdict"] == "met"
    # 102 unsent x Rs 9.125
    assert r["refund"]["unsent_credits"] == 102
    assert r["refund"]["amount_cents"] == 93075


def test_without_a_report_date_the_check_stays_open(db):
    r = campaign_refund_check(db, 1, now=NOW)
    assert _cond(r, 5) == "open" and _cond(r, 6) == "open"
    assert r["verdict"] == "open"


def test_fix_window_still_running_is_open(db):
    r = campaign_refund_check(db, 1, reported_on=date(2026, 10, 3), now=NOW)
    assert _cond(r, 6) == "open"
    assert r["verdict"] == "open"


def test_sending_resumed_after_report_means_fixed(db):
    db.add(EmailSent(campaign_id=1, status="sent", followup_number=0, sent_at=datetime(2026, 9, 30, 10)))
    db.commit()
    r = campaign_refund_check(db, 1, reported_on=date(2026, 9, 28), now=NOW)
    assert _cond(r, 6) == "no"
    assert r["verdict"] == "not_met"


def test_gmail_disconnection_in_streak_fails_condition_2(db):
    db.add(SystemEvent(event_type="campaign_paused", created_at=datetime(2026, 9, 20, 8),
                       meta={"campaign_id": 1, "pause_reason": "gmail_auth", "paused_by": "system"}))
    db.commit()
    r = campaign_refund_check(db, 1, reported_on=date(2026, 9, 28), now=NOW)
    assert _cond(r, 2) == "no"
    assert _cond(r, 1) == "check"
    assert r["verdict"] == "not_met"


def test_short_gap_is_not_a_total_failure(db):
    # Sends every few days: never 7 zero days in a row.
    for d in range(19, 36, 4):
        db.add(EmailSent(campaign_id=1, status="sent", followup_number=0, sent_at=datetime(2026, 9, 1) + timedelta(days=d)))
    db.commit()
    r = campaign_refund_check(db, 1, reported_on=date(2026, 9, 28), now=NOW)
    assert _cond(r, 3) == "no"


def test_late_report_fails_condition_5(db):
    r = campaign_refund_check(db, 1, reported_on=date(2026, 10, 5), now=datetime(2026, 10, 20))
    assert _cond(r, 5) == "no"


def test_followups_break_a_streak_but_dont_use_credits(db):
    db.add(EmailSent(campaign_id=1, status="sent", followup_number=1, sent_at=datetime(2026, 9, 25, 10)))
    db.commit()
    r = campaign_refund_check(db, 1, reported_on=date(2026, 9, 28), now=NOW)
    assert r["campaign"]["first_touch_sent"] == 98
    assert r["streak"]["start"] == "2026-09-26"


def test_partial_refund_moves_only_the_unsent_value(db, monkeypatch):
    calls = []

    async def fake_provider(order, reason, amount_cents=None):
        calls.append(amount_cents)
        return "rfnd_1"

    monkeypatch.setattr(refunds, "_provider_refund", fake_provider)
    monkeypatch.setattr("services.refund_check.datetime", _FrozenDatetime)
    out = asyncio.run(refunds.refund_campaign_unsent(db, 1, reported_on=date(2026, 9, 28), actor="admin", reason="§3.3"))
    assert calls == [93075]
    order = db.get(PaymentOrder, 9)
    assert order.refunded_cents == 93075 and order.status == "paid"
    assert db.get(Campaign, 1).status == "cancelled"
    assert out["credits_revoked"] == 102
    # The user keeps exactly the 98 credits that were used: no spendable
    # leftovers alongside the money.
    wallet = db.query(UserCredit).filter_by(user_id="u").one()
    assert (wallet.total_credits, wallet.used_credits) == (98, 98)


def test_partial_refund_refuses_when_33_is_not_met(db, monkeypatch):
    async def fake_provider(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("provider called")

    monkeypatch.setattr(refunds, "_provider_refund", fake_provider)
    monkeypatch.setattr("services.refund_check.datetime", _FrozenDatetime)
    with pytest.raises(refunds.RefundError, match="not met"):
        asyncio.run(refunds.refund_campaign_unsent(db, 1, reported_on=date(2026, 10, 3), actor="admin", reason="x"))
    assert db.get(PaymentOrder, 9).refunded_cents is None


class _FrozenDatetime(datetime):
    @classmethod
    def utcnow(cls):
        return NOW
