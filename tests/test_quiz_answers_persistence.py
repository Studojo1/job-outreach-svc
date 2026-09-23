"""The quiz must leave a trace on the server before its last question.

Until migration 044 the only write in the whole quiz was in the completion
branch of POST /candidate/{id}/chat/stream. Everything before it lived in the
browser, replayed to the server on each turn and thrown away again. Three
things followed, and all three were measured in production:

    1,559 abandoned quizzes stored zero answers, so per-question drop-off
    could not be computed at all.

    outreach_orders.quiz_started_at was set on 1 row out of 4,791, because
    the stage was gated on a "__start__" bootstrap message that no client
    sends: the frontend serves Q1 from a local constant and only calls the
    endpoint once the user has answered it.

    A retry that left a duplicate user message in the replayed history shifted
    every later answer onto the wrong question key, because the replay assigns
    answers by array position.

These tests guard the two properties that fix buys: answers are keyed by
question key (never by position), and a short replay cannot erase what the
server already holds. That second one is what makes retry safe to add — the
frontend audit wants an automatic retry on the quiz stream, and a retry that
replays a truncated history must not delete the earlier answers.
"""
import pathlib
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import Base, Candidate


# Same shim as test_send_order.py: the models are Postgres-shaped and SQLite
# has no JSONB. Rendering it as JSON keeps these tests running against the REAL
# Candidate definition rather than a stand-in that could drift from production.
@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Candidate.__table__])
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _persist(db, candidate, answers):
    """The merge from routes_candidate.candidate_chat_stream.

    Kept identical to the code under test on purpose: if the endpoint's merge
    changes and this does not, the assertions below stop describing production.
    Returns is_first_answer, which is what now drives the quiz_started stage.
    """
    is_first_answer = False
    if answers:
        stored = candidate.quiz_answers if isinstance(candidate.quiz_answers, dict) else {}
        merged = {**stored, **answers}
        if merged != stored:
            is_first_answer = not stored
            candidate.quiz_answers = merged
            db.commit()
    return is_first_answer


@pytest.fixture()
def candidate(db):
    c = Candidate(id=1, user_id="u1", resume_text="...")
    db.add(c)
    db.commit()
    return c


def test_abandoned_quiz_keeps_the_answers_it_did_give(db, candidate):
    """The headline bug: abandon at Q3 and the server used to hold nothing."""
    _persist(db, candidate, {"career_stage": "Student", "clarity": "Exactly"})
    _persist(db, candidate, {"career_stage": "Student", "clarity": "Exactly",
                             "job_type": "Internship"})
    # User closes the tab here — no completion branch ever runs.
    db.expire_all()
    assert db.get(Candidate, 1).quiz_answers == {
        "career_stage": "Student",
        "clarity": "Exactly",
        "job_type": "Internship",
    }


def test_short_replay_cannot_erase_stored_answers(db, candidate):
    """A truncated replay must merge, not replace.

    This is the precondition for adding retry to the quiz stream fetch. A retry
    whose client lost part of its history replays fewer answers; if that
    overwrote the row, the retry would destroy the quiz it was meant to rescue.
    """
    _persist(db, candidate, {"career_stage": "Student", "clarity": "Exactly",
                             "job_type": "Internship"})
    _persist(db, candidate, {"career_stage": "Student"})  # client lost history
    db.expire_all()
    stored = db.get(Candidate, 1).quiz_answers
    assert stored["job_type"] == "Internship", "a short replay erased an answer"
    assert stored["clarity"] == "Exactly"


def test_answers_are_keyed_by_question_key_not_position(db, candidate):
    """A later answer updates its own key and disturbs no other.

    Position-keyed storage is the off-by-one the frontend audit found: a retry
    leaves a duplicate user message, every later answer shifts by one, and the
    corruption is silent. Keying by question key is what removes it.
    """
    _persist(db, candidate, {"career_stage": "Student", "clarity": "Exactly"})
    _persist(db, candidate, {"clarity": "Still figuring out"})  # user changed it
    db.expire_all()
    assert db.get(Candidate, 1).quiz_answers == {
        "career_stage": "Student",
        "clarity": "Still figuring out",
    }


def test_quiz_started_fires_once_on_the_first_stored_answer(db, candidate):
    """mark_stage is one-shot, but it should not be asked to fire every turn."""
    assert _persist(db, candidate, {"career_stage": "Student"}) is True
    assert _persist(db, candidate, {"career_stage": "Student",
                                    "clarity": "Exactly"}) is False
    assert _persist(db, candidate, {"clarity": "Exactly"}) is False


def test_no_answers_yet_writes_nothing(db, candidate):
    """The very first request carries one answer; a zero-answer turn is a
    bootstrap and must not stamp a started-at or create an empty dict."""
    assert _persist(db, candidate, {}) is False
    db.expire_all()
    assert db.get(Candidate, 1).quiz_answers is None
