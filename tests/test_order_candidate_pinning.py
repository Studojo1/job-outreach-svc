"""An order that has generated leads must keep the candidate that owns them.

Production, before this fix: 88 orders pointed at a candidate row holding no
leads, while the same user's leads sat on a sibling row. Five of those users
had paid. 928 users have more than one candidate row; the worst has 190.

The sequence that produces it:

    upload resume          -> candidate A, order.candidate_id = A
    generate leads         -> leads written against A, leads_generated_at set
    upload another resume  -> candidate B, order.candidate_id = B   <-- here

That last step was an unconditional re-point in get_or_create_active_order.
Every downstream reader walks order -> candidate -> leads, so after it the
user's own dashboard finds nothing: the leads are still on A.

The rule these tests pin down is deliberately narrow. Re-pointing BEFORE leads
exist is correct — a user who re-uploads before generating wants the new
resume used, and breaking that would be a worse bug than the one being fixed.
Only leads_generated_at freezes the link.
"""
import pathlib
import sys
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import Base, OutreachOrder
from services.stage_tracking import get_or_create_active_order


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


def _order(db, candidate_id, leads_generated=False):
    o = OutreachOrder(
        user_id="u1",
        candidate_id=candidate_id,
        status="created",
        leads_generated_at=datetime.utcnow() if leads_generated else None,
        action_log=[],
    )
    db.add(o)
    db.commit()
    return o


def test_order_with_leads_keeps_its_candidate(db):
    """The 88-order bug: a re-upload must not strand generated leads."""
    order = _order(db, candidate_id=1, leads_generated=True)
    got = get_or_create_active_order(db, "u1", candidate_id=2)
    assert got.id == order.id
    assert got.candidate_id == 1, "order was re-pointed away from its own leads"


def test_order_without_leads_still_follows_a_new_upload(db):
    """Re-uploading before generating should use the new resume, as before."""
    order = _order(db, candidate_id=1, leads_generated=False)
    got = get_or_create_active_order(db, "u1", candidate_id=2)
    assert got.id == order.id
    assert got.candidate_id == 2


def test_an_order_with_no_candidate_yet_is_always_linkable(db):
    """Stage 1 creates the order before a candidate exists; linking it later
    must keep working even once leads somehow exist."""
    order = _order(db, candidate_id=None, leads_generated=True)
    got = get_or_create_active_order(db, "u1", candidate_id=7)
    assert got.id == order.id
    assert got.candidate_id == 7


def test_same_candidate_is_a_no_op(db):
    order = _order(db, candidate_id=5, leads_generated=True)
    got = get_or_create_active_order(db, "u1", candidate_id=5)
    assert got.candidate_id == 5
    assert got.id == order.id
