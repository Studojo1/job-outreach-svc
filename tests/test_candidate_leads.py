"""GET /candidate/{id}/leads: ranking, paging and the justification-only poll.

The endpoint used to return every lead with no ORDER BY and a sort on score
alone, so tied leads came back in heap order and moved between polls. It also
had no way to ask for less than the whole ~800-row set.
"""
import pathlib
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import Base, Candidate, Lead, LeadScore
from api.routes_candidate import get_candidate_leads


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine, tables=[t.__table__ for t in (Candidate, Lead, LeadScore)]
    )
    session = sessionmaker(bind=engine)()
    session.add(Candidate(id=1, user_id="u1", resume_text="..."))
    # id -> score. 3 and 2 tie on purpose; 5 is unscored.
    for lead_id, score in [(1, 50.5), (2, 80.0), (3, 80.0), (4, 80.4), (5, None)]:
        session.add(Lead(id=lead_id, candidate_id=1, name=f"l{lead_id}", company="c"))
        if score is not None:
            session.add(LeadScore(
                lead_id=lead_id, overall_score=score, title_relevance=0,
                department_relevance=0, industry_relevance=0, seniority_relevance=0,
                location_relevance=0, explanation="same on every row",
                justification_json={"bullets": [f"b{lead_id}"]},
            ))
    session.commit()
    yield session
    session.close()


USER = SimpleNamespace(id="u1")


def _call(db, **kw):
    kw.setdefault("limit", None)
    kw.setdefault("offset", 0)
    kw.setdefault("fields", None)
    return get_candidate_leads(1, current_user=USER, db=db, **kw)


def test_default_returns_everything_ranked_with_id_tiebreak(db):
    resp = _call(db)
    assert [l["id"] for l in resp["leads"]] == [4, 2, 3, 1, 5]
    assert resp["total"] == 5
    # The decimal survives, so 80.4 outranks the two 80.0s.
    assert resp["leads"][0]["score"]["overall"] == pytest.approx(80.4)


def test_default_drops_fields_no_client_reads(db):
    score = _call(db)["leads"][0]["score"]
    assert set(score) == {"overall", "justification"}


def test_limit_offset_pages_the_ranked_list_and_total_is_unpaged(db):
    resp = _call(db, limit=2, offset=1)
    assert [l["id"] for l in resp["leads"]] == [2, 3]
    assert resp["total"] == 5
    assert [l["id"] for l in _call(db, offset=3)["leads"]] == [1, 5]


def test_justification_mode_is_id_and_score_only(db):
    resp = _call(db, fields="justification")
    assert [l["id"] for l in resp["leads"]] == [4, 2, 3, 1, 5]
    assert set(resp["leads"][0]) == {"id", "score"}
    assert resp["leads"][0]["score"]["justification"] == {"bullets": ["b4"]}
    assert resp["leads"][-1]["score"] is None


def test_someone_elses_candidate_is_404(db):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        get_candidate_leads(1, limit=None, offset=0, fields=None,
                            current_user=SimpleNamespace(id="other"), db=db)
    assert exc.value.status_code == 404
