"""scripts/backfill_order_links: P12 payment links / promotion and P10 closure."""
import pathlib
import sys
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import Base, OutreachOrder, PaymentOrder
from scripts.backfill_order_links import run


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[OutreachOrder.__table__, PaymentOrder.__table__])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _order(db, oid, status, created, **kw):
    db.add(OutreachOrder(id=oid, user_id="u", status=status, created_at=created, action_log=[], **kw))


def _pay(db, pid, created, amount=170000, **kw):
    db.add(PaymentOrder(id=pid, user_id="u", status="paid", amount_cents=amount, currency="INR",
                        tier=200, created_at=created, **kw))


def test_links_the_payment_to_the_order_active_when_they_paid_and_promotes(db):
    _order(db, 1, "leads_ready", datetime(2026, 5, 1))
    _order(db, 2, "created", datetime(2026, 6, 1))          # a later re-onboarding
    _pay(db, 10, datetime(2026, 5, 6))
    db.commit()
    out = run(db, apply=True)
    assert db.get(PaymentOrder, 10).outreach_order_id == 1
    assert db.get(OutreachOrder, 2).status == "campaign_setup"  # latest order promoted
    assert out["payments_linked"] == 1 and out["orders_promoted"] == 1


def test_coupon_orders_are_left_alone(db):
    _order(db, 1, "created", datetime(2026, 5, 1))
    _pay(db, 10, datetime(2026, 5, 6), amount=0)
    db.commit()
    assert run(db, apply=True) == {"payments_linked": 0, "orders_promoted": 0, "orders_completed": 0}


def test_completes_orders_whose_finished_campaign_is_gone(db):
    _order(db, 1, "campaign_running", datetime(2026, 5, 1), campaign_completed_at=datetime(2026, 6, 1))
    _order(db, 2, "campaign_running", datetime(2026, 5, 1))  # never completed: untouched
    db.commit()
    run(db, apply=True)
    assert db.get(OutreachOrder, 1).status == "completed"
    assert db.get(OutreachOrder, 2).status == "campaign_running"


def test_dry_run_changes_nothing(db):
    _order(db, 1, "created", datetime(2026, 5, 1))
    _pay(db, 10, datetime(2026, 5, 6))
    db.commit()
    assert run(db, apply=False)["payments_linked"] == 1
    assert db.get(PaymentOrder, 10).outreach_order_id is None
    assert db.get(OutreachOrder, 1).status == "created"
