"""B2C audit 30 Sep 2026: campaign rows re-verified after the 29 Sep fixes.

PP-P26 (legacy campaigns with no reservation are capped going forward and
re-reserve from the wallet on resume), PS-N14 (no mailbox address in auth
errors), PS-N03 (first-100 reply rate by launch week). Every test drives
production code.
"""
import pathlib
import sys
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import (  # noqa: E402
    Base, Campaign, Candidate, CreditLedger, EmailAccount, EmailSent, Lead, LeadScore,
    OutreachOrder, PaymentOrder, SuppressedEmail, User, UserCredit,
)
from services.email_campaign import campaign_service, campaign_worker  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


NOW = datetime(2026, 9, 30, 12, 0, 0)


@pytest.fixture()
def db(monkeypatch):
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
        # 150 credits free.
        UserCredit(user_id="u", total_credits=350, used_credits=200),
        # A legacy campaign: created before per-campaign reservations.
        Campaign(id=34, candidate_id=1, email_account_id=5, name="legacy", status="paused",
                 daily_limit=20, user_timezone="Asia/Kolkata", credits_reserved=0, credits_released=0),
    ])
    s.commit()
    # Resume side effects that are not under test here.
    monkeypatch.setattr(campaign_worker, "check_mailbox_replies", lambda *a, **k: (0, 0))
    monkeypatch.setattr(campaign_worker, "compute_campaign_schedule", lambda *a, **k: None)
    yield s
    s.close()


def _legacy_rows(db, delivered: int, unsent: int, status="pending_enrichment"):
    base = datetime.utcnow()
    for i in range(delivered):
        db.add(EmailSent(campaign_id=34, to_email=f"d{i}@x.com", status="sent", followup_number=0,
                         sent_at=base - timedelta(days=30)))
    for i in range(unsent):
        db.add(EmailSent(campaign_id=34, to_email=f"p{i}@x.com", status=status, followup_number=0,
                         scheduled_at=base + timedelta(minutes=i)))
    db.commit()


def _used(db):
    return db.query(UserCredit).filter_by(user_id="u").one().used_credits


# ── PP-P26: legacy campaigns are capped going forward ─────────────────────

def test_resuming_a_legacy_campaign_reserves_what_the_wallet_covers(db):
    _legacy_rows(db, delivered=2, unsent=200)
    campaign_service.transition_campaign(db, 34, "running")
    c = db.get(Campaign, 34)
    assert c.status == "running"
    assert c.credits_reserved == 2 + 150          # delivered baseline + wallet
    assert _used(db) == 350                         # the 150 free credits are now held
    retired = db.query(EmailSent).filter_by(campaign_id=34, status="expired").all()
    assert len(retired) == 50 and all(r.error_message == campaign_worker.OVER_CAP_MESSAGE for r in retired)
    # The best-scheduled 150 stay queued; the latest 50 were retired.
    kept = db.query(EmailSent).filter_by(campaign_id=34, status="pending_enrichment").all()
    assert max(r.scheduled_at for r in kept) < min(r.scheduled_at for r in retired)
    ledger = db.query(CreditLedger).filter_by(campaign_id=34).one()
    assert ledger.delta_used == 150


def test_after_adoption_the_worker_caps_at_the_new_reservation(db, monkeypatch):
    _legacy_rows(db, delivered=2, unsent=200)
    campaign_service.transition_campaign(db, 34, "running")
    c = db.get(Campaign, 34)
    row = db.query(EmailSent).filter_by(campaign_id=34, status="pending_enrichment").first()
    assert campaign_worker._over_paid_cap(db, c, row, campaign_worker._DELIVERED) is False
    # Once 150 more are delivered the campaign is at its cap.
    for r in db.query(EmailSent).filter_by(campaign_id=34, status="pending_enrichment").all():
        r.status = "sent"
    db.commit()
    extra = EmailSent(campaign_id=34, to_email="late@x.com", status="queued", followup_number=0)
    db.add(extra)
    db.commit()
    assert campaign_worker._over_paid_cap(db, c, extra, campaign_worker._DELIVERED) is True


def test_legacy_resume_with_no_free_credits_is_refused_and_changes_nothing(db):
    db.query(UserCredit).update({"used_credits": 350})
    db.commit()
    _legacy_rows(db, delivered=2, unsent=10)
    with pytest.raises(ValueError, match="no free credits"):
        campaign_service.transition_campaign(db, 34, "running")
    db.rollback()
    c = db.get(Campaign, 34)
    assert (c.status, c.credits_reserved) == ("paused", 0)
    assert db.query(EmailSent).filter_by(campaign_id=34, status="pending_enrichment").count() == 10


def test_a_campaign_with_a_reservation_is_not_re_reserved(db):
    c = db.get(Campaign, 34)
    c.credits_reserved = 20
    db.commit()
    _legacy_rows(db, delivered=2, unsent=10)
    campaign_service.transition_campaign(db, 34, "running")
    assert db.get(Campaign, 34).credits_reserved == 20 and _used(db) == 200


def test_restarting_a_cancelled_legacy_campaign_keeps_its_delivered_baseline(db):
    c = db.get(Campaign, 34)
    c.status = "cancelled"
    db.commit()
    _legacy_rows(db, delivered=40, unsent=100, status="expired")
    campaign_service.transition_campaign(db, 34, "running")
    c = db.get(Campaign, 34)
    # 40 already delivered stay counted, 100 restored on new credits: the cap
    # must not treat the 40 as eating into the 100 just paid for.
    assert c.credits_reserved == 140
    assert _used(db) == 300
    row = db.query(EmailSent).filter_by(campaign_id=34, status="pending_enrichment").first()
    assert campaign_worker._over_paid_cap(db, c, row, campaign_worker._DELIVERED) is False


def test_admin_restart_applies_the_same_cap(db):
    from api.routes_admin import campaign_restart_admin
    db.query(UserCredit).update({"used_credits": 350})
    db.commit()
    _legacy_rows(db, delivered=0, unsent=5)
    with pytest.raises(HTTPException) as e:
        campaign_restart_admin(34, admin=db.get(User, "u"), db=db)
    assert e.value.status_code == 400
    assert db.get(Campaign, 34).status == "paused"


def test_gmail_reconnect_resumes_the_others_when_one_cannot(db):
    from api.routes_gmail import _resume_auth_paused_campaigns
    db.query(UserCredit).update({"used_credits": 350})           # nothing free
    db.add(Campaign(id=45, candidate_id=1, email_account_id=5, name="paid", status="paused",
                    pause_reason="gmail_auth", credits_reserved=50, credits_released=0))
    db.get(Campaign, 34).pause_reason = "gmail_auth"
    db.add(EmailSent(campaign_id=45, to_email="q@x.com", status="queued", followup_number=0))
    db.commit()
    _legacy_rows(db, delivered=0, unsent=5)
    _resume_auth_paused_campaigns(db, "u", "u@gmail.com")
    assert db.get(Campaign, 45).status == "running"
    assert db.get(Campaign, 34).status == "paused"


# ── PS-N14: auth errors never carry the mailbox address ───────────────────

def test_revoked_token_error_names_the_account_not_the_address(db, monkeypatch):
    from services.email_campaign import gmail_send_service

    class _Revoked:
        ok, status_code, text = False, 400, ""

        def json(self):
            return {"error": "invalid_grant"}

    monkeypatch.setattr(gmail_send_service.requests, "post", lambda *a, **k: _Revoked())
    account = db.get(EmailAccount, 5)
    account.token_expiry = datetime.utcnow() - timedelta(hours=1)
    with pytest.raises(gmail_send_service.GmailAuthError) as e:
        gmail_send_service._refresh_token_sync(account, db)
    assert "u@gmail.com" not in str(e.value) and "account 5" in str(e.value)
    assert account.token_invalid_at is not None
    # Still recognised as an auth failure for the customer-facing reason.
    assert campaign_service.customer_failure_reason("failed", str(e.value)) is not None


# ── PS-N03: first-100 reply rate by launch week ────────────────────────────

def test_first_100_reply_rate_is_grouped_by_launch_week(db):
    from services.reply_rate_cohorts import first_100_reply_rate_by_launch_week
    now = datetime(2026, 9, 30, 12)
    monday = datetime(2026, 9, 7, 10)            # a Monday, 23 days ago
    db.add_all([
        Campaign(id=1, candidate_id=1, name="a", status="running", started_at=monday),
        Campaign(id=2, candidate_id=1, name="b", status="running", started_at=monday + timedelta(days=2)),
    ])
    # Campaign 1: 120 first emails, 4 replies inside the first 100, 5 after.
    for i in range(120):
        db.add(EmailSent(campaign_id=1, to_email=f"a{i}@x.com", followup_number=0,
                         status="replied" if (i < 4 or i >= 115) else "sent",
                         sent_at=monday + timedelta(hours=i)))
    # Campaign 2: 50 first emails, none replied; one test email that replied.
    for i in range(50):
        db.add(EmailSent(campaign_id=2, to_email=f"b{i}@x.com", followup_number=0, status="sent",
                         sent_at=monday + timedelta(days=2, hours=i)))
    db.add(EmailSent(campaign_id=2, to_email="t@x.com", followup_number=0, status="replied", is_test=True,
                     sent_at=monday + timedelta(days=2)))
    db.commit()
    out = first_100_reply_rate_by_launch_week(db, weeks=8, now=now)
    [week] = out["weeks"]
    assert week["week_start"] == "2026-09-07"
    assert (week["campaigns"], week["emails"], week["replies"]) == (2, 150, 4)
    assert week["reply_rate_pct"] == round(4 / 150 * 100, 2)
    assert week["below_alert"] is False
    assert {c["campaign_id"]: c["emails"] for c in week["by_campaign"]} == {1: 100, 2: 50}


def test_a_week_under_the_alert_line_is_flagged_and_young_emails_wait(db):
    from services.reply_rate_cohorts import first_100_reply_rate_by_launch_week
    now = datetime(2026, 9, 30, 12)
    launch = datetime(2026, 9, 1, 9)
    db.add(Campaign(id=1, candidate_id=1, name="a", status="running", started_at=launch))
    for i in range(100):
        db.add(EmailSent(campaign_id=1, to_email=f"a{i}@x.com", followup_number=0,
                         status="replied" if i == 0 else "sent",
                         # the last 20 were sent 5 days ago: too young to count
                         sent_at=launch + timedelta(hours=i) if i < 80 else now - timedelta(days=5)))
    db.commit()
    [week] = first_100_reply_rate_by_launch_week(db, weeks=8, now=now)["weeks"]
    assert week["emails"] == 80 and week["replies"] == 1
    assert week["below_alert"] is True                # 1.25% < 1.5%
