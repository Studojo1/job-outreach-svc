"""Re-uploading must not scatter a user across candidate rows.

POST /candidate/upload inserted a new candidate row every time it was called.
A double-tapped button, a retried request, or a student re-uploading to fix a
typo each produced another row: 939 users have more than one and the worst has
190.

The extras are not inert. The quiz answers land on whichever row the client
happens to be holding while the order points at a different one, which is the
same split that stranded 88 orders' leads from the orders that paid for them.

The rule under test is deliberately narrow, because the failure modes are
asymmetric. Reusing a row that has already produced targeting or leads would
destroy work the student did; failing to reuse an untouched row merely leaves a
harmless empty row behind. So reuse happens only when the row is recent AND has
nothing derived from it yet.
"""
import pathlib
import sys
from datetime import datetime, timedelta

import pytest
from sqlalchemy import cast, create_engine, or_, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import Base, Candidate, Lead


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Candidate.__table__, Lead.__table__])
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _find_reusable(db, user_id):
    """The lookup from routes_candidate.upload_resume.

    Kept identical to the code under test on purpose: if the endpoint's query
    changes and this does not, the assertions below stop describing production.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=30)
    # A JSONB column stores Python None as the JSON value `null`, not SQL NULL,
    # so IS NULL alone matches nothing. This mirrors the endpoint exactly.
    no_targeting = or_(
        Candidate.target_roles.is_(None),
        cast(Candidate.target_roles, Text) == "null",
        cast(Candidate.target_roles, Text) == "[]",
    )
    return (
        db.query(Candidate)
        .outerjoin(Lead, Lead.candidate_id == Candidate.id)
        .filter(
            Candidate.user_id == user_id,
            Candidate.created_at >= cutoff,
            no_targeting,
            Lead.id.is_(None),
        )
        .order_by(Candidate.created_at.desc())
        .first()
    )


def _candidate(db, cid, user="u1", minutes_ago=1, target_roles=None):
    c = Candidate(
        id=cid,
        user_id=user,
        resume_text="old resume",
        created_at=datetime.utcnow() - timedelta(minutes=minutes_ago),
        target_roles=target_roles,
    )
    db.add(c)
    db.commit()
    return c


def test_a_fresh_untouched_row_is_reused(db):
    """The double-tap case: two uploads seconds apart should share one row."""
    _candidate(db, 1, minutes_ago=0)
    assert _find_reusable(db, "u1").id == 1


def test_a_row_with_targeting_is_never_reused(db):
    """It represents a real attempt. Overwriting it destroys the student's quiz."""
    _candidate(db, 1, minutes_ago=1, target_roles=["Backend Engineer"])
    assert _find_reusable(db, "u1") is None


def test_a_row_with_leads_is_never_reused(db):
    """Same reasoning, and these are the leads someone may have paid for."""
    _candidate(db, 1, minutes_ago=1)
    db.add(Lead(id=1, candidate_id=1, name="a lead"))
    db.commit()
    assert _find_reusable(db, "u1") is None


def test_an_old_row_is_not_reused(db):
    """Past the window this is a deliberate new attempt, not a retry."""
    _candidate(db, 1, minutes_ago=45)
    assert _find_reusable(db, "u1") is None


def test_another_users_row_is_never_reused(db):
    """The worst possible outcome: one student's resume on another's row."""
    _candidate(db, 1, user="u2", minutes_ago=0)
    assert _find_reusable(db, "u1") is None


def test_the_most_recent_reusable_row_wins(db):
    _candidate(db, 1, minutes_ago=20)
    _candidate(db, 2, minutes_ago=2)
    assert _find_reusable(db, "u1").id == 2


def test_no_rows_at_all_means_insert(db):
    assert _find_reusable(db, "u1") is None


def test_a_used_row_does_not_mask_a_reusable_one(db):
    """A user with history should still get their fresh empty row reused."""
    _candidate(db, 1, minutes_ago=5, target_roles=["Backend Engineer"])
    _candidate(db, 2, minutes_ago=1)
    assert _find_reusable(db, "u1").id == 2


def test_an_empty_target_roles_list_still_counts_as_unused(db):
    """74 rows have `[]` rather than NULL, from the same era as the other
    data-integrity bugs. An empty list is no more real targeting than a missing
    one, so those rows are reusable too."""
    _candidate(db, 1, minutes_ago=1, target_roles=[])
    assert _find_reusable(db, "u1").id == 1
