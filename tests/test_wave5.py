"""Post-payment audit wave 5: deliverability and money hardening."""
import asyncio
import pathlib
import sys
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import (
    Base, Campaign, Candidate, CreditLedger, EmailAccount, EmailSent, Lead, LeadScore,
    OutreachOrder, PaymentOrder, SuppressedEmail, User, UserCredit,
)
from services import reconcile
from services.email_campaign import campaign_worker, suppression


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


NOW = datetime(2026, 9, 29, 12, 0, 0)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (
        User, Candidate, Lead, LeadScore, Campaign, EmailAccount, EmailSent, OutreachOrder,
        PaymentOrder, UserCredit, CreditLedger, SuppressedEmail)])
    s = sessionmaker(bind=engine)()
    s.add_all([
        User(id="u", email="u@x.com", name="U", email_verified=True, created_at=NOW, updated_at=NOW),
        Candidate(id=1, user_id="u", resume_text="."),
        EmailAccount(id=5, user_id="u", email_address="u@gmail.com", provider="gmail",
                     access_token="t", refresh_token="r", token_expiry=datetime.utcnow() + timedelta(days=1)),  # noqa: S106
        UserCredit(user_id="u", total_credits=200, used_credits=200),
        Campaign(id=10, candidate_id=1, email_account_id=5, name="a", status="running", daily_limit=20,
                 credits_reserved=200, credits_released=0),
        Campaign(id=11, candidate_id=1, email_account_id=5, name="b", status="running", daily_limit=20,
                 credits_reserved=0, credits_released=0),
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


# ── P18 suppression ──────────────────────────────────────────────────────────

def test_suppressed_address_is_never_sent_and_its_credit_returns(db, monkeypatch):
    sent = []
    _mock_send(monkeypatch, sent)
    suppression.suppress(db, "Bounced@Example.com ", "bounce")
    db.add(EmailSent(id=1, campaign_id=10, to_email="bounced@example.com", subject="s", body="b",
                     status="queued", scheduled_at=datetime.utcnow() - timedelta(minutes=1), enrichment_status="enriched"))
    db.commit()
    campaign_worker._send_ready(db)
    assert sent == []
    assert db.get(EmailSent, 1).status == "failed"
    assert db.query(UserCredit).one().used_credits == 199


def test_suppress_is_idempotent_and_normalised(db):
    suppression.suppress(db, "A@B.com", "x")
    suppression.suppress(db, "a@b.com ", "y")
    db.commit()
    assert db.query(SuppressedEmail).count() == 1
    assert suppression.is_suppressed(db, "A@b.COM")


# ── P35/P37 one budget per mailbox ─────────────────────────────────────────

def test_two_campaigns_on_one_mailbox_share_one_daily_budget(db, monkeypatch):
    sent = []
    _mock_send(monkeypatch, sent)
    monkeypatch.setattr(campaign_worker, "_daily_target", lambda c: 3)
    monkeypatch.setattr(campaign_worker, "MAX_SEND_PER_CYCLE", 50)
    now = datetime.utcnow()
    for i, cid in enumerate([10, 10, 11, 11, 11, 10]):
        db.add(EmailSent(campaign_id=cid, to_email=f"l{i}@x.com", subject="s", body="b", status="queued",
                         scheduled_at=now - timedelta(minutes=10 - i), enrichment_status="enriched"))
    db.commit()
    campaign_worker._send_ready(db)
    assert len(sent) == 3
    held = db.query(EmailSent).filter_by(status="queued").all()
    assert len(held) == 3 and all(e.scheduled_at > now for e in held)  # moved, not left at the head


# ── P19 replies land on Touch 1 ──────────────────────────────────────────────

def test_follow_up_is_cancelled_when_a_sibling_in_the_thread_bounced(db, monkeypatch):
    sent = []
    _mock_send(monkeypatch, sent)
    root = EmailSent(id=1, campaign_id=10, to_email="l@x.com", subject="s", body="b", status="sent",
                     thread_id="T", sent_at=datetime.utcnow() - timedelta(days=5), enrichment_status="enriched")
    t2 = EmailSent(id=2, campaign_id=10, to_email="l@x.com", status="bounced", followup_number=1,
                   parent_email_id=1, thread_id="T")
    t3 = EmailSent(id=3, campaign_id=10, to_email="l@x.com", status="followup_pending", followup_number=2,
                   parent_email_id=1, scheduled_at=datetime.utcnow() - timedelta(minutes=1), enrichment_status="enriched")
    db.add_all([root, t2, t3])
    db.commit()
    campaign_worker._process_followups(db)
    assert sent == [] and db.get(EmailSent, 3).status == "cancelled_reply"


def test_reply_on_a_follow_up_cancels_the_next_one(db, monkeypatch):
    sent = []
    _mock_send(monkeypatch, sent)
    db.add_all([
        EmailSent(id=1, campaign_id=10, to_email="l@x.com", subject="s", body="b", status="sent", thread_id="T"),
        EmailSent(id=2, campaign_id=10, to_email="l@x.com", status="sent", followup_number=1, parent_email_id=1,
                  thread_id="T", reply_received_at=NOW),
        EmailSent(id=3, campaign_id=10, to_email="l@x.com", status="followup_pending", followup_number=2,
                  parent_email_id=1, scheduled_at=datetime.utcnow() - timedelta(minutes=1), enrichment_status="enriched"),
    ])
    db.commit()
    campaign_worker._process_followups(db)
    assert sent == [] and db.get(EmailSent, 3).status == "cancelled_reply"


# ── P05 refunds ─────────────────────────────────────────────────────────────

def _paid(db, provider="razorpay", credits_granted=200):
    db.add(PaymentOrder(id=50, user_id="u", status="paid", provider=provider, amount_cents=182500,
                        currency="INR", tier=200, credits_granted=credits_granted,
                        razorpay_payment_id="pay_1", dodo_payment_id="pd_1", created_at=NOW))
    db.commit()


def test_refund_cancels_campaigns_and_revokes_credits(db, monkeypatch):
    from services import refunds
    _paid(db)
    for _ in range(150):
        db.add(EmailSent(campaign_id=10, status="pending_enrichment", enrichment_status="pending"))
    for _ in range(50):
        db.add(EmailSent(campaign_id=10, status="sent"))
    db.commit()
    async def ok(order, reason):
        return "rfnd_1"
    monkeypatch.setattr(refunds, "_provider_refund", ok)
    out = asyncio.run(refunds.refund_payment(db, 50, actor="admin", reason="never received service"))
    order = db.get(PaymentOrder, 50)
    assert (order.status, order.refunded_cents, order.refund_id) == ("refunded", 182500, "rfnd_1")
    assert db.get(Campaign, 10).status == "cancelled"
    assert out["credits_revoked"] == 150 and out["credits_already_used"] == 50
    w = db.query(UserCredit).one()
    assert (w.total_credits, w.used_credits) == (50, 50)


def test_provider_refusal_changes_nothing(db, monkeypatch):
    from services import refunds
    _paid(db)
    async def boom(order, reason):
        raise RuntimeError("provider said no")
    monkeypatch.setattr(refunds, "_provider_refund", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(refunds.refund_payment(db, 50, actor="admin", reason="x"))
    assert db.get(PaymentOrder, 50).status == "paid"
    assert db.get(Campaign, 10).status == "running"
    assert db.query(UserCredit).one().total_credits == 200


def test_coupon_and_refunded_orders_are_refused(db):
    from services import refunds
    db.add(PaymentOrder(id=51, user_id="u", status="paid", provider="coupon", amount_cents=0, currency="INR", tier=200))
    db.add(PaymentOrder(id=52, user_id="u", status="refunded", provider="dodo", amount_cents=2700, currency="USD", tier=350))
    db.commit()
    for oid in (51, 52):
        with pytest.raises(refunds.RefundError):
            asyncio.run(refunds.refund_payment(db, oid, actor="a", reason="x"))


# ── P16 paid but never credited ──────────────────────────────────────────────

def test_paid_order_that_never_granted_is_credited(db, monkeypatch):
    monkeypatch.setattr(reconcile, "_tell_founders", lambda *a: None)
    db.add(User(id="g", email="g@x.com", name="G", email_verified=True, created_at=NOW, updated_at=NOW))
    db.add(PaymentOrder(id=340, user_id="g", status="paid", provider="dodo", amount_cents=2700, currency="USD",
                        tier=350, plan_id="email_350", credits_granted=0, created_at=NOW - timedelta(days=100)))
    db.commit()
    assert reconcile.grant_paid_without_credits(db, NOW) == 1
    assert db.query(UserCredit).filter_by(user_id="g").one().total_credits == 350
    assert reconcile.grant_paid_without_credits(db, NOW) == 0  # once


# ── P45 ──────────────────────────────────────────────────────────────────────

def test_replacement_cap_rounds_down():
    import math
    from services.email_campaign.replenishment import REPLACEMENT_CAP_PERCENT
    assert math.floor(50 * REPLACEMENT_CAP_PERCENT) == 12


def test_dead_mailbox_in_reply_check_is_a_warning_not_a_traceback(db, monkeypatch, caplog):
    import logging
    from services.email_campaign.gmail_send_service import GmailAuthError

    def dead(account, db_):
        raise GmailAuthError("Gmail auth expired — u@gmail.com must reconnect their Gmail account")
    monkeypatch.setattr(campaign_worker, "_refresh_token_sync", dead)
    monkeypatch.setattr(campaign_worker, "_last_reply_check", 0, raising=False)
    db.add(EmailSent(campaign_id=10, to_email="l@x.com", status="sent", thread_id="T", sent_at=NOW))
    db.commit()
    with caplog.at_level(logging.WARNING):
        campaign_worker._check_replies(db)
    assert not any(r.exc_info for r in caplog.records if "REPLY_CHECK" in r.getMessage())
