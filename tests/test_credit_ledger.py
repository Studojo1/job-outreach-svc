"""services/credits.py: every wallet change is ledgered, and releases cannot mint.

The invariant the whole post-payment fix leans on: for each user,
sum(delta_total) == total_credits and sum(delta_used) == used_credits.
"""
import pathlib
import sys

import pytest
from sqlalchemy import create_engine, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import Base, Campaign, Candidate, CreditLedger, UserCredit
from services import credits


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine, tables=[t.__table__ for t in (Candidate, Campaign, UserCredit, CreditLedger)]
    )
    session = sessionmaker(bind=engine)()
    session.add_all([
        Candidate(id=1, user_id="u", resume_text="."),
        Campaign(id=10, candidate_id=1, name="c"),
    ])
    session.commit()
    yield session
    session.close()


def _wallet(db):
    return db.query(UserCredit).filter_by(user_id="u").one()


def _assert_ledger_matches_wallet(db):
    total, used = db.query(func.sum(CreditLedger.delta_total), func.sum(CreditLedger.delta_used)) \
        .filter(CreditLedger.user_id == "u").one()
    w = _wallet(db)
    assert (total or 0, used or 0) == (w.total_credits, w.used_credits)


def test_grant_creates_the_wallet_and_ledgers_it(db):
    credits.grant(db, "u", 200, credits.GRANT_PAYMENT, payment_order_id=None)
    db.commit()
    assert _wallet(db).total_credits == 200
    _assert_ledger_matches_wallet(db)


def test_reserve_refuses_more_than_available_and_changes_nothing(db):
    credits.grant(db, "u", 50, credits.GRANT_PAYMENT)
    assert credits.reserve(db, "u", 51, credits.RESERVE_CAMPAIGN) is None
    db.commit()
    assert _wallet(db).used_credits == 0
    assert db.query(CreditLedger).filter_by(reason=credits.RESERVE_CAMPAIGN).count() == 0


def test_reserve_then_attach_records_the_campaign(db):
    credits.grant(db, "u", 200, credits.GRANT_PAYMENT)
    entry = credits.reserve(db, "u", 200, credits.RESERVE_CAMPAIGN)
    db.commit()
    campaign = db.get(Campaign, 10)
    credits.attach_campaign(entry, campaign)
    db.commit()
    assert campaign.credits_reserved == 200
    assert entry.campaign_id == 10
    _assert_ledger_matches_wallet(db)


def test_release_is_capped_at_what_the_campaign_still_holds(db):
    credits.grant(db, "u", 400, credits.GRANT_PAYMENT)
    campaign = db.get(Campaign, 10)
    credits.reserve(db, "u", 100, credits.RESERVE_CAMPAIGN, campaign=campaign)
    credits.reserve(db, "u", 100, credits.RESERVE_ENRICHMENT)  # someone else's reservation
    assert credits.release(db, "u", 60, credits.RELEASE_SEND_FAILED, campaign=campaign) == 60
    # Only 40 left on this campaign, even though the wallet has 140 used.
    assert credits.release(db, "u", 500, credits.RELEASE_CAMPAIGN_FINISHED, campaign=campaign) == 40
    assert credits.release(db, "u", 1, credits.RELEASE_CAMPAIGN_FINISHED, campaign=campaign) == 0
    db.commit()
    assert _wallet(db).used_credits == 100
    assert campaign.credits_released == 100
    _assert_ledger_matches_wallet(db)


def test_release_never_takes_used_below_zero(db):
    credits.grant(db, "u", 10, credits.GRANT_PAYMENT)
    credits.reserve(db, "u", 5, credits.RESERVE_ENRICHMENT)
    assert credits.release(db, "u", 50, credits.RELEASE_ENRICHMENT_UNUSED) == 5
    db.commit()
    assert _wallet(db).used_credits == 0
    _assert_ledger_matches_wallet(db)


def test_release_without_a_wallet_is_a_no_op(db):
    assert credits.release(db, "u", 5, credits.RELEASE_ADMIN) == 0
    assert db.query(CreditLedger).count() == 0


def test_legacy_helpers_are_ledgered(db):
    from api.routes_payment import _grant_credits, deduct_credits, refund_credits
    _grant_credits(db, "u", 100)
    assert deduct_credits(db, "u", 30) is True
    assert deduct_credits(db, "u", 1000) is False
    assert refund_credits(db, "u", 10) == 10
    db.commit()
    assert (_wallet(db).total_credits, _wallet(db).used_credits) == (100, 20)
    assert [r.reason for r in db.query(CreditLedger).order_by(CreditLedger.id)] == [
        credits.GRANT_PAYMENT, credits.RESERVE_ENRICHMENT, credits.RELEASE_ENRICHMENT_UNUSED]
    _assert_ledger_matches_wallet(db)
