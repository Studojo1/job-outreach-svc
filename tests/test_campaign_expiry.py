"""An 8-day plan's campaign must get an expiry when it first launches.

transition_campaign read OutreachOrder before a later local
`from database.models import OutreachOrder` in the same function. Python
treats the name as local to the whole function, so the first read raised
UnboundLocalError. It was caught and logged, expires_at was never set, and
every email_50 (8-day) campaign since June ran with no expiry. Found by
ruff F823 when lint was made a required check (28 Sep 2026).
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from database.models import Base


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


@pytest.fixture(autouse=True)
def _no_scheduler(monkeypatch):
    # Scheduling is not under test and needs the worker's full environment.
    import services.email_campaign.campaign_worker as worker
    monkeypatch.setattr(worker, "compute_campaign_schedule", lambda db, campaign_id: None)


@pytest.fixture()
def db():
    from database.models import (
        Campaign, Candidate, EmailAccount, EmailSent, Lead, LeadScore, OutreachOrder, PaymentOrder, User,
    )
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        User.__table__, Candidate.__table__, Lead.__table__, LeadScore.__table__,
        EmailAccount.__table__, Campaign.__table__, EmailSent.__table__,
        OutreachOrder.__table__, PaymentOrder.__table__,
    ])
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _seed(db, plan_id):
    from database.models import Campaign, Candidate, OutreachOrder, PaymentOrder, User
    now = datetime.utcnow()
    db.add(User(id="u1", email="u1@example.test", name="U", created_at=now, updated_at=now))
    db.add(Candidate(id=1, user_id="u1", resume_text="r"))
    db.add(Campaign(id=10, candidate_id=1, status="draft", name="c"))
    db.flush()
    order = OutreachOrder(user_id="u1", candidate_id=1, campaign_id=10)
    db.add(order)
    db.flush()
    # One queued email, so the launch is allowed.
    from database.models import EmailSent
    db.add(EmailSent(campaign_id=10, status="queued", to_email="a@example.test"))
    db.add(PaymentOrder(user_id="u1", outreach_order_id=order.id, status="paid",
                        plan_id=plan_id, amount_cents=49900, currency="INR", tier=50))
    db.commit()


def test_first_launch_of_an_8_day_plan_sets_expiry(db):
    from database.models import Campaign
    from services.email_campaign.campaign_service import transition_campaign

    _seed(db, "email_50")
    before = datetime.utcnow()
    transition_campaign(db, 10, "running")
    c = db.query(Campaign).get(10)
    assert c.started_at is not None
    assert c.expires_at is not None, "8-day plan launched with no expiry"
    assert timedelta(days=7, hours=23) < c.expires_at - before < timedelta(days=8, minutes=5)


def test_a_plan_without_a_duration_gets_no_expiry(db):
    from database.models import Campaign
    from services.email_campaign.campaign_service import transition_campaign

    _seed(db, "email_200")
    transition_campaign(db, 10, "running")
    assert db.query(Campaign).get(10).expires_at is None
