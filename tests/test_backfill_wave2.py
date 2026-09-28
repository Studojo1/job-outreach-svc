"""The wave-2 backfill settles the past without minting credits."""
import pathlib
import sys
from datetime import datetime

import pytest
from sqlalchemy import create_engine, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import (
    Base, Campaign, Candidate, CreditLedger, EmailSent, Lead, LeadScore, OutreachOrder, UserCredit,
)
from scripts.backfill_wave2_settlement import run


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (
        Candidate, Lead, LeadScore, Campaign, EmailSent, OutreachOrder, UserCredit, CreditLedger)])
    s = sessionmaker(bind=engine)()
    s.add(Candidate(id=1, user_id="u", resume_text="."))
    s.commit()
    yield s
    s.close()


def _rows(db, cid, status, n, **kw):
    for _ in range(n):
        db.add(EmailSent(campaign_id=cid, status=status, enrichment_status="enriched", **kw))


def _used(db):
    return db.query(UserCredit).filter_by(user_id="u").one().used_credits


def test_campaign_100_shape(db):
    """Paid 500, 1 delivered, the rest killed by a dead Gmail grant, marked completed."""
    db.add(UserCredit(user_id="u", total_credits=500, used_credits=500))
    db.add(Campaign(id=100, candidate_id=1, name="c", status="completed", created_at=datetime(2026, 6, 25)))
    db.add(OutreachOrder(id=9, user_id="u", campaign_id=100, status="campaign_running", action_log=[]))
    _rows(db, 100, "sent", 1)
    _rows(db, 100, "failed", 441, error_message="Token refresh failed: Gmail auth expired — x must reconnect")
    _rows(db, 100, "failed", 58, error_message="Apollo could not find email for this contact")
    db.commit()

    report = run(db, apply=True)["u"]
    c = db.get(Campaign, 100)
    assert (c.status, c.pause_reason) == ("paused", "gmail_auth")   # reopened, resumable
    assert report["auth_requeued"] == 441
    assert db.query(EmailSent).filter_by(campaign_id=100, status="queued").count() == 441
    assert db.query(EmailSent).filter(EmailSent.campaign_id == 100,
                                      EmailSent.scheduled_at.isnot(None),
                                      EmailSent.status == "queued").count() == 0
    assert report["released_failed"] == 58                        # no-match slots come back
    assert _used(db) == 442
    # reopened, so its order is not closed
    assert db.get(OutreachOrder, 9).status == "campaign_running"


def test_completed_campaign_with_unsent_work_is_settled_and_closed(db):
    db.add(UserCredit(user_id="u", total_credits=200, used_credits=200))
    db.add(Campaign(id=45, candidate_id=1, name="c", status="completed", created_at=datetime(2026, 5, 1)))
    db.add(OutreachOrder(id=8, user_id="u", campaign_id=45, status="campaign_running", action_log=[]))
    _rows(db, 45, "sent", 180)
    _rows(db, 45, "queued", 20)
    db.commit()
    report = run(db, apply=True)["u"]
    assert report["released_unsent"] == 20 and _used(db) == 180
    assert db.query(EmailSent).filter_by(campaign_id=45, status="expired").count() == 20
    assert db.get(OutreachOrder, 8).status == "completed"


def test_unpaid_rows_cannot_mint_credits(db):
    """Campaign 124: 1,064 first touches against a 200-credit wallet."""
    db.add(UserCredit(user_id="u", total_credits=200, used_credits=200))
    db.add(Campaign(id=124, candidate_id=1, name="c", status="completed", created_at=datetime(2026, 8, 21)))
    _rows(db, 124, "sent", 181)
    _rows(db, 124, "queued", 883)
    db.commit()
    run(db, apply=True)
    c = db.get(Campaign, 124)
    assert c.credits_reserved == 200
    assert c.credits_released <= 200
    assert _used(db) >= 0
    total_used_delta = db.query(func.sum(CreditLedger.delta_used)).scalar()
    assert -total_used_delta <= 200


def test_replaced_failures_and_follow_ups_return_nothing(db):
    db.add(UserCredit(user_id="u", total_credits=10, used_credits=10))
    db.add(Campaign(id=7, candidate_id=1, name="c", status="running", created_at=datetime(2026, 9, 1)))
    db.commit()
    original = EmailSent(campaign_id=7, status="failed", enrichment_status="skipped",
                         error_message="Apollo could not find email for this contact")
    db.add(original)
    db.commit()
    db.add(EmailSent(campaign_id=7, status="sent", replacement_for_id=original.id,
                     replacement_reason="enrichment_exhausted"))
    _rows(db, 7, "failed", 3, followup_number=1, error_message="whatever")
    db.commit()
    run(db, apply=True)
    assert _used(db) == 10


def test_dry_run_changes_nothing(db):
    db.add(UserCredit(user_id="u", total_credits=200, used_credits=200))
    db.add(Campaign(id=45, candidate_id=1, name="c", status="completed", created_at=datetime(2026, 5, 1)))
    _rows(db, 45, "queued", 20)
    db.commit()
    report = run(db, apply=False)["u"]
    assert report["released_unsent"] == 20
    assert _used(db) == 200
    assert db.query(CreditLedger).count() == 0
    assert db.get(Campaign, 45).credits_reserved is None
