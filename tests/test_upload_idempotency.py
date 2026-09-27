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
from sqlalchemy import create_engine
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
    # The production lookup, not a copy of it: a copy "kept identical on
    # purpose" is exactly what let the endpoint and these tests drift.
    from api.routes_candidate import find_reusable_candidate
    return find_reusable_candidate(db, user_id)


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


def test_an_old_unused_row_is_still_reused(db):
    """There is no age limit any more (quiz audit Q20/Q39, 27 Sep re-check).

    A 30-minute window still left 8% of recent users with duplicate rows: a
    student who uploads, leaves before finishing the quiz and comes back the
    next day is the same attempt. An unused row has nothing worth keeping.
    """
    _candidate(db, 1, minutes_ago=60 * 24 * 3)
    assert _find_reusable(db, "u1").id == 1


def test_an_old_used_row_is_still_protected(db):
    _candidate(db, 1, minutes_ago=60 * 24 * 3, target_roles=["Backend Engineer"])
    assert _find_reusable(db, "u1") is None


def test_reusing_a_row_forgets_the_previous_quiz(db):
    """Answers given for the old resume must not merge into the new quiz,
    whose questions are built from a different resume."""
    from api.routes_candidate import reset_candidate_for_new_resume

    c = _candidate(db, 1, minutes_ago=5)
    c.quiz_answers = {"career_stage": "Student, not graduating soon"}
    c.quiz_answers_updated_at = datetime.utcnow()
    c.parsed_json = {"_qps": {"domain": "engineering"}}
    c.resume_profile = {"domain": "engineering"}
    c.dream_companies = ["Google"]
    db.commit()

    reset_candidate_for_new_resume(c, "new resume", {"name": "A"})
    db.commit()
    db.refresh(c)
    assert c.resume_text == "new resume"
    assert c.parsed_json == {"name": "A"}          # frozen _qps snapshot gone too
    assert c.resume_profile is None
    assert c.quiz_answers is None
    assert c.quiz_answers_updated_at is None
    assert c.dream_companies is None


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
