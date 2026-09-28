"""B2C open items UC-Q24 and PP-P39 (pricing page, server side).

- UC-Q24: create-order sold email credits to users with no leads at all.
- PP-P39: /payment/credits said has_active_campaign for paused campaigns too,
  so the pricing page read "campaign running" for a paused one.
"""
import asyncio
import pathlib
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api import routes_payment
from database.models import Base, Campaign, Candidate, EmailSent, Lead, LeadScore, UserCredit


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (
        Candidate, Lead, LeadScore, Campaign, EmailSent, UserCredit)])
    s = sessionmaker(bind=engine)()
    s.add_all([Candidate(id=1, user_id="u", resume_text="."), Candidate(id=2, user_id="u", resume_text=".")])
    s.commit()
    yield s
    s.close()


class _U:
    id = "u"


def test_no_leads_and_no_drafts_means_nothing_to_send(db):
    db.execute(text("CREATE TABLE extension_drafts (id INTEGER PRIMARY KEY, user_id TEXT)"))
    assert routes_payment._has_something_to_send(db, "u") is False


def test_leads_on_a_sibling_candidate_count(db):
    db.add(Lead(candidate_id=2, name="HM", company="Acme"))
    db.commit()
    assert routes_payment._has_something_to_send(db, "u") is True


def test_extension_users_can_buy_without_leads(db):
    db.execute(text("CREATE TABLE extension_drafts (id INTEGER PRIMARY KEY, user_id TEXT)"))
    db.execute(text("INSERT INTO extension_drafts (user_id) VALUES ('u')"))
    assert routes_payment._has_something_to_send(db, "u") is True


def test_a_failed_check_never_blocks_payment(db):
    # no extension_drafts table at all: the query errors, the payment goes on
    assert routes_payment._has_something_to_send(db, "u") is True


@pytest.mark.parametrize("statuses, expected", [
    (["paused"], "paused"),
    (["running"], "running"),
    (["paused", "running"], "running"),
    (["completed"], None),
])
def test_credits_reports_campaign_status(db, statuses, expected):
    db.add(UserCredit(user_id="u", total_credits=200, used_credits=200))
    for i, st in enumerate(statuses):
        db.add(Campaign(id=10 + i, candidate_id=1, name="c", status=st))
    db.commit()
    out = asyncio.run(routes_payment.get_credits(current_user=_U(), db=db))
    assert out["campaign_status"] == expected
    assert out["has_active_campaign"] is (expected is not None)
