"""Picking the right candidate for a stranded order, or refusing to pick.

scripts/reconcile_stranded_orders.py rewrites production rows for users who
have already paid, so the interesting behaviour is not the happy path — it is
whether the script knows when it does NOT know. 928 users have more than one
candidate row and the worst has 190, so "the user's other candidate" is often
not a single row. A wrong guess silently hands a paying user someone else's
lead list.

These tests pin the rule and, more importantly, the refusals.
"""
import pathlib
import sys
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import Base, Candidate, Lead, OutreachOrder
from scripts.reconcile_stranded_orders import _pick_candidate


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


T0 = datetime(2026, 6, 1, 12, 0, 0)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[t.__table__ for t in (Candidate, Lead, OutreachOrder)],
    )
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _candidate(db, cid, user="u1"):
    db.add(Candidate(id=cid, user_id=user, resume_text="x"))
    db.commit()


def _leads(db, candidate_id, n, at):
    start = db.query(Lead).count()
    for i in range(n):
        db.add(Lead(id=start + i + 1, candidate_id=candidate_id, name=f"l{i}", created_at=at))
    db.commit()


def _order(db, candidate_id, leads_generated_at=T0, user="u1"):
    o = OutreachOrder(
        user_id=user, candidate_id=candidate_id, status="created",
        leads_generated_at=leads_generated_at, action_log=[],
    )
    db.add(o)
    db.commit()
    return o


def test_single_sibling_with_leads_is_chosen(db):
    _candidate(db, 1); _candidate(db, 2)
    _leads(db, 1, 50, T0 - timedelta(minutes=5))
    order = _order(db, candidate_id=2)
    target, reason = _pick_candidate(db, order)
    assert target == 1
    assert "only sibling" in reason


def test_no_sibling_with_leads_is_refused(db):
    """Nothing to recover: the leads are not on another row either."""
    _candidate(db, 1); _candidate(db, 2)
    order = _order(db, candidate_id=2)
    target, reason = _pick_candidate(db, order)
    assert target is None
    assert "no sibling candidate has leads" in reason


def test_closest_batch_at_or_before_generation_wins(db):
    """The order paid for the batch generated at its own timestamp, not a later one."""
    for cid in (1, 2, 3, 4):
        _candidate(db, cid)
    _leads(db, 1, 10, T0 - timedelta(days=30))   # an old, unrelated batch
    _leads(db, 2, 10, T0 - timedelta(minutes=2))  # this order's batch
    _leads(db, 3, 10, T0 + timedelta(days=5))     # generated AFTER: not this one
    order = _order(db, candidate_id=4, leads_generated_at=T0)
    target, _ = _pick_candidate(db, order)
    assert target == 2


def test_indistinguishable_batches_are_refused(db):
    """Two batches seconds apart cannot be told apart, so do not guess."""
    for cid in (1, 2, 3):
        _candidate(db, cid)
    _leads(db, 1, 10, T0 - timedelta(seconds=10))
    _leads(db, 2, 10, T0 - timedelta(seconds=20))
    order = _order(db, candidate_id=3, leads_generated_at=T0)
    target, reason = _pick_candidate(db, order)
    assert target is None
    assert "within 60s" in reason


def test_all_batches_after_generation_are_refused(db):
    """Leads newer than the order's own generation belong to a later order."""
    for cid in (1, 2, 3):
        _candidate(db, cid)
    _leads(db, 1, 10, T0 + timedelta(days=1))
    _leads(db, 2, 10, T0 + timedelta(days=2))
    order = _order(db, candidate_id=3, leads_generated_at=T0)
    target, reason = _pick_candidate(db, order)
    assert target is None
    assert "none at or before" in reason


def test_another_users_candidate_is_never_chosen(db):
    """The scan is per user. Handing over another user's leads is the worst outcome."""
    _candidate(db, 1, user="u2")
    _leads(db, 1, 99, T0 - timedelta(minutes=1))
    _candidate(db, 2, user="u1")
    order = _order(db, candidate_id=2, user="u1")
    target, reason = _pick_candidate(db, order)
    assert target is None
    assert "no sibling candidate has leads" in reason
