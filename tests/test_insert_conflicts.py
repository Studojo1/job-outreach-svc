"""UC-Q36 prep: lead and score inserts tolerate the new unique indexes.

leads(candidate_id, apollo_id) and lead_scores(lead_id) get UNIQUE indexes.
Two concurrent passes both pass the check-then-insert, so the second insert
must be skipped, not crash the page or the scoring batch.
"""
import pathlib
import sys

import pytest
from sqlalchemy import create_engine, false, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import Base, Candidate, Lead, LeadScore
from services.lead_discovery import lead_collector_service as lc


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Candidate.__table__, Lead.__table__, LeadScore.__table__])
    with engine.begin() as c:
        # The indexes the UC-Q36 migration adds.
        c.execute(text("CREATE UNIQUE INDEX uq_leads_cand_apollo ON leads (candidate_id, apollo_id) "
                       "WHERE apollo_id IS NOT NULL"))
        c.execute(text("CREATE UNIQUE INDEX uq_lead_scores_lead ON lead_scores (lead_id)"))
    session = sessionmaker(bind=engine)()
    session.add(Candidate(id=1, user_id="u1", resume_text="...", target_roles=["Product Manager"]))
    session.commit()
    yield session
    session.close()


def _person(pid):
    return {"id": pid, "first_name": "N", "last_name": pid, "title": "Product Manager",
            "organization": {"name": f"co-{pid}"}}


def test_store_people_skips_a_row_a_concurrent_run_inserted(db, monkeypatch):
    db.add(Lead(candidate_id=1, apollo_id="p1", name="N p1", company="co-p1"))
    db.commit()
    # Simulate the race: the dedupe check ran before the other run's insert
    # landed, so it sees nothing.
    monkeypatch.setattr(lc, "or_", lambda *c: false())
    collected = lc._store_people([_person("p1"), _person("p2")], 1, 800, db, 0)
    db.commit()
    assert collected == 1  # only the real insert counts
    assert sorted(a for (a,) in db.query(Lead.apollo_id)) == ["p1", "p2"]


def test_scoring_skips_leads_a_concurrent_pass_scored(db, monkeypatch):
    from api import routes_discovery as rd
    import services.lead_scoring.lead_scoring_service as svc
    monkeypatch.setattr(rd, "JUSTIFY_TOP_K", 0)  # no LLM pass in this test
    db.add_all([Lead(id=1, candidate_id=1, apollo_id="a", name="a", title="Product Manager", company="A"),
                Lead(id=2, candidate_id=1, apollo_id="b", name="b", title="Product Manager", company="B")])
    db.commit()

    real = svc.score_and_select_leads

    def racing(*a, **k):
        out = real(*a, **k)
        # Another pass commits a score for lead 1 while this one is scoring.
        db.add(LeadScore(lead_id=1, overall_score=12.0, title_relevance=0, department_relevance=0,
                         industry_relevance=0, seniority_relevance=0, location_relevance=0,
                         explanation="other pass"))
        db.commit()
        return out
    monkeypatch.setattr(svc, "score_and_select_leads", racing)

    assert rd._score_candidate_leads(db, db.get(Candidate, 1)) == 1
    rows = {s.lead_id: s for s in db.query(LeadScore)}
    assert set(rows) == {1, 2}
    assert rows[1].explanation == "other pass"  # the first writer wins
