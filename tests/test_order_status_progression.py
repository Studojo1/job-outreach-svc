"""The order moves forward as discovery runs, and a paid-for order is never
left at a pre-payment status.

Before: nothing wrote leads_generating or leads_ready, so the order created at
resume upload sat at 'created' through discovery; the later
leads_ready -> campaign_setup update 400'd and was swallowed. Paid orders were
repaired by _finalize_credits, but 100%-coupon and credit-covered checkouts
never reached it.
"""
import pathlib
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import Base, OutreachOrder
from services.stage_tracking import advance_discovery_status, promote_paid_order


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[OutreachOrder.__table__])
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_discovery_creates_and_binds_an_order_when_none_exists(db):
    order = advance_discovery_status(db, "u1", 7, "leads_generating")
    assert order.status == "leads_generating"
    assert order.candidate_id == 7


def test_discovery_moves_created_through_to_leads_ready(db):
    db.add(OutreachOrder(user_id="u1", candidate_id=7, status="created", action_log=[]))
    db.commit()
    advance_discovery_status(db, "u1", 7, "leads_generating")
    order = advance_discovery_status(db, "u1", 7, "leads_ready")
    assert order.status == "leads_ready"
    assert db.query(OutreachOrder).count() == 1


@pytest.mark.parametrize("later", ["leads_ready", "campaign_setup", "campaign_running"])
def test_discovery_never_moves_an_order_backwards(db, later):
    db.add(OutreachOrder(user_id="u1", candidate_id=7, status=later, action_log=[]))
    db.commit()
    assert advance_discovery_status(db, "u1", 7, "leads_generating").status == later


def test_order_pinned_to_another_candidates_leads_is_left_alone(db):
    from datetime import datetime
    db.add(OutreachOrder(user_id="u1", candidate_id=3, status="created",
                         leads_generated_at=datetime.utcnow(), action_log=[]))
    db.commit()
    order = advance_discovery_status(db, "u1", 7, "leads_generating")
    assert (order.candidate_id, order.status) == (3, "created")


@pytest.mark.parametrize("status,moved", [
    ("created", True), ("leads_generating", True), ("leads_ready", True),
    ("campaign_setup", False), ("campaign_running", False),
])
def test_promote_paid_order(status, moved):
    order = OutreachOrder(user_id="u1", status=status, action_log=[])
    assert promote_paid_order(order, "test") is moved
    assert order.status == ("campaign_setup" if moved else status)


def test_promote_paid_order_tolerates_no_order():
    assert promote_paid_order(None, "test") is False
