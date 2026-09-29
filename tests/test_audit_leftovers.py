"""Post-payment audit: the remaining prescribed parts of partial rows."""
import asyncio
import pathlib
import sys
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import (
    Base, Campaign, Candidate, Coupon, CreditLedger, EmailAccount, EmailSent, EnrichmentJob, Lead,
    LeadScore, OutreachOrder, PaymentOrder, SuppressedEmail, SystemEvent, User, UserCredit,
)
from services import credits, reconcile
from services.email_campaign import campaign_worker


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


NOW = datetime.utcnow()


@pytest.fixture()
def engine():
    return create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)


@pytest.fixture()
def db(engine, monkeypatch):
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (
        User, Candidate, Lead, LeadScore, Campaign, EmailAccount, EmailSent, OutreachOrder, PaymentOrder,
        UserCredit, CreditLedger, SuppressedEmail, SystemEvent, EnrichmentJob, Coupon)])
    s = sessionmaker(bind=engine)()
    s.add_all([
        User(id="u", email="u@x.com", name="U", email_verified=True, created_at=NOW, updated_at=NOW),
        Candidate(id=1, user_id="u", resume_text="."),
        EmailAccount(id=5, user_id="u", email_address="u@gmail.com", provider="gmail",
                     access_token="t", refresh_token="r",  # noqa: S106
                     token_expiry=NOW + timedelta(days=1)),
    ])
    s.commit()
    monkeypatch.setattr(reconcile, "_tell_founders", lambda *a: None)
    yield s
    s.close()


def _wallet(db, total, used):
    db.add(UserCredit(user_id="u", total_credits=total, used_credits=used))
    db.commit()


# P01
def test_campaign_that_delivered_under_half_is_degraded(db):
    _wallet(db, 10, 10)
    db.add(Campaign(id=1, candidate_id=1, email_account_id=5, name="c", status="running",
                    credits_reserved=10, credits_released=0))
    for st in ["sent"] * 2 + ["failed"] * 8:
        db.add(EmailSent(campaign_id=1, status=st))
    db.commit()
    campaign_worker.finish_campaign(db, db.get(Campaign, 1), reason="t")
    assert db.get(Campaign, 1).outcome == "degraded"


def test_healthy_campaign_is_delivered(db):
    db.add(Campaign(id=1, candidate_id=1, email_account_id=5, name="c", status="running"))
    for st in ["sent"] * 9 + ["failed"]:
        db.add(EmailSent(campaign_id=1, status=st))
    db.commit()
    campaign_worker.finish_campaign(db, db.get(Campaign, 1), reason="t")
    assert db.get(Campaign, 1).outcome == "delivered"


# P03
def test_create_links_by_campaign_and_leaves_the_old_pointer_alone(db):
    from api.routes_campaign import CampaignCreateRequest, api_create_campaign
    from api.routes_orders import _serialize_order
    _wallet(db, 100, 0)
    db.add(Candidate(id=2, user_id="u", resume_text=".", parsed_json={"career_analysis": {}}))
    for i in range(60):
        db.add(Lead(candidate_id=2, name=f"l{i}"))
    db.add(OutreachOrder(id=9, user_id="u", status="campaign_setup", action_log=[], created_at=NOW))
    db.commit()

    class U:
        id = "u"
        name = "U"
    out = asyncio.run(api_create_campaign(CampaignCreateRequest(
        candidate_id=2, email_account_id=5, name="c", lead_limit=50, selected_styles=["ai"]),
        current_user=U(), db=db))
    order = db.get(OutreachOrder, 9)
    assert order.campaign_id is None                                   # not written any more
    assert db.get(Campaign, out["campaign_id"]).outreach_order_id == 9
    assert _serialize_order(order)["order"]["campaign_id"] == out["campaign_id"]  # still reported


# P14
def test_second_redemption_of_a_coupon_is_refused(db):
    from api.routes_payment import _already_redeemed
    db.add(Coupon(id=3, code="FREE", discount_type="percent", discount_value=100, max_uses=10, uses=1, is_active=True))
    db.add(PaymentOrder(id=1, user_id="u", status="paid", provider="coupon", amount_cents=0, currency="INR",
                        tier=200, coupon_id=3))
    db.commit()
    assert _already_redeemed(db, 3, "u") is True
    assert _already_redeemed(db, 3, "someone-else") is False
    # Google's reviewers' coupon stays usable by the same account (cap still applies).
    db.add(Coupon(id=4, code="OAuth100", discount_type="percent", discount_value=100, max_uses=32, uses=22, is_active=True))
    db.add(PaymentOrder(id=2, user_id="u", status="paid", provider="coupon", amount_cents=0, currency="INR",
                        tier=200, coupon_id=4))
    db.commit()
    assert _already_redeemed(db, 4, "u") is False


# P24
def test_cancelled_campaign_restarts_with_credits_reserved_again(db, monkeypatch):
    from services.email_campaign import campaign_service
    monkeypatch.setattr(campaign_worker, "compute_campaign_schedule", lambda d, c, **kw: None)
    _wallet(db, 50, 5)
    db.add(Campaign(id=1, candidate_id=1, email_account_id=5, name="c", status="cancelled",
                    credits_reserved=50, credits_released=45, outcome="cancelled"))
    for _ in range(5):
        db.add(EmailSent(campaign_id=1, status="sent"))
    for _ in range(45):
        db.add(EmailSent(campaign_id=1, status="expired"))
    db.commit()
    campaign_service.transition_campaign(db, 1, "running")
    c = db.get(Campaign, 1)
    assert c.status == "running" and c.outcome is None
    assert db.query(EmailSent).filter_by(campaign_id=1, status="pending_enrichment").count() == 45
    assert db.query(UserCredit).one().used_credits == 50


def test_cancelled_campaign_without_free_credits_cannot_restart(db):
    from services.email_campaign import campaign_service
    _wallet(db, 50, 50)
    db.add(Campaign(id=1, candidate_id=1, email_account_id=5, name="c", status="cancelled", credits_reserved=50))
    db.add(EmailSent(campaign_id=1, status="expired"))
    db.commit()
    with pytest.raises(ValueError):
        campaign_service.transition_campaign(db, 1, "running")
    assert db.get(Campaign, 1).status == "cancelled"


# P29
def test_wallet_that_drifts_from_the_ledger_is_reported(db, monkeypatch):
    told = []
    monkeypatch.setattr(reconcile, "_tell_founders", lambda subject, message: told.append(subject))
    credits.grant(db, "u", 100, credits.GRANT_PAYMENT)
    db.commit()
    assert reconcile.check_ledger(db, NOW) == 0
    db.query(UserCredit).update({"total_credits": 150})   # bypassed the ledger
    db.commit()
    assert reconcile.check_ledger(db, NOW) == 1 and told
    reconcile.check_ledger(db, NOW + timedelta(hours=1))
    assert len(told) == 1  # at most daily


# P31
def test_enrichment_jobs_persist_and_a_dead_job_gives_its_credits_back(db, engine, monkeypatch):
    from api import routes_enrichment
    monkeypatch.setattr(routes_enrichment, "SessionLocal", sessionmaker(bind=engine))
    _wallet(db, 200, 200)
    routes_enrichment._enrichment_jobs["j1"] = {"user_id": "u", "reserved": 200, "status": "processing", "total": 200}
    job = routes_enrichment._enrichment_jobs["j1"]
    job["enriched"] = 30                              # persisted on assignment
    assert routes_enrichment._enrichment_jobs.get("j1")["enriched"] == 30
    db.query(EnrichmentJob).update({"updated_at": NOW - timedelta(hours=2)})
    db.commit()
    assert reconcile.release_dead_enrichment_jobs(db, NOW) == 170
    assert db.query(UserCredit).one().used_credits == 30
    assert reconcile.release_dead_enrichment_jobs(db, NOW) == 0  # once


# P34
def test_exhausted_lead_is_not_sent_to_apollo_again(db, monkeypatch):
    calls = []
    monkeypatch.setattr("services.enrichment.enrichment_service.enrich_single_lead_classified",
                        lambda lead: calls.append(lead.id))
    db.add(Campaign(id=1, candidate_id=1, email_account_id=5, name="c", status="running"))
    db.add(Lead(id=7, candidate_id=1, name="x", enrichment_fail_count=3))
    db.add(EmailSent(campaign_id=1, lead_id=7, status="pending_enrichment", enrichment_status="pending",
                     scheduled_at=NOW))
    db.commit()
    campaign_worker._enrich_upcoming(db)
    assert calls == []
    assert db.query(EmailSent).one().status == "failed"


# P40
def test_pause_writes_an_event_row_without_a_user_id(db):
    from services.email_campaign import campaign_service
    db.add(Campaign(id=1, candidate_id=1, email_account_id=5, name="c", status="running"))
    db.commit()
    campaign_service.transition_campaign(db, 1, "paused")
    ev = db.query(SystemEvent).filter_by(event_type="campaign_paused").one()
    assert ev.user_id is None and ev.meta["campaign_id"] == 1 and ev.meta["owner_user_id"] == "u"


# P46
def test_follow_up_into_a_deleted_thread_is_sent_fresh(db, monkeypatch):
    from services.email_campaign.gmail_send_service import GmailSendError
    sent = []

    def fake(**kw):
        if kw.get("thread_id"):
            raise GmailSendError("Gmail send failed: 404: Requested entity was not found", 404)
        sent.append(kw)
        return {"id": "m", "threadId": "new"}
    monkeypatch.setattr(campaign_worker, "send_gmail_email", fake)
    monkeypatch.setattr(campaign_worker, "_ensure_tracking_token", lambda e: None)
    monkeypatch.setattr(campaign_worker, "ph_capture", lambda *a, **k: None)
    monkeypatch.setattr(campaign_worker, "fetch_message_id_header", lambda *a: None, raising=False)
    monkeypatch.setattr("services.email_campaign.gmail_send_service.fetch_message_id_header", lambda *a: None)
    db.add(Campaign(id=1, candidate_id=1, email_account_id=5, name="c", status="running"))
    db.add(Lead(id=8, candidate_id=1, name="l"))
    db.add(EmailSent(id=1, campaign_id=1, lead_id=8, to_email="l@x.com", subject="s", body="b", status="sent",
                     thread_id="gone", sent_at=NOW - timedelta(days=5)))
    db.add(EmailSent(id=2, campaign_id=1, lead_id=8, to_email="l@x.com", body="fu", status="followup_pending", followup_number=1,
                     parent_email_id=1, scheduled_at=NOW - timedelta(minutes=1), enrichment_status="enriched"))
    db.commit()
    monkeypatch.setattr(campaign_worker, "generate_followup_body", lambda *a, **k: "fu", raising=False)
    campaign_worker._process_followups(db)
    assert len(sent) == 1 and "thread_id" not in sent[0]
    assert db.get(EmailSent, 2).status == "sent"
