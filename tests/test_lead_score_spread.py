"""B2C UC-Q04: lead scores must discriminate between leads.

60% of every score was a fixed 2.7/10 company rating (discovery never passes
ratings), so 98% of production scores sat in a 16-57 band. And location was 0
on every row, because Apollo's people search returns no city for a person.
"""
import pathlib
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import Base, Candidate, Lead
from services.lead_discovery import lead_collector_service as lc
from services.lead_scoring.lead_scoring_service import score_and_select_leads
from services.shared.schemas.filter_schema import LeadFilter
from services.shared.schemas.target_segment_schema import TargetSegment


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


PROFILE = {
    "preferred_roles": ["Product Manager"],
    "target_roles": ["Product Manager"],
    "location_preferences": ["Bengaluru"],
    "company_preferences": {},
}
ROLE_INTEL = {"candidate_seniority": "entry", "departments": ["product"]}


def _score(leads, **kw):
    out = score_and_select_leads(
        [dict(ld) for ld in leads], PROFILE, ROLE_INTEL, target_count=len(leads), **kw,
    )
    return {ld["apollo_person_id"]: ld for ld in out}


def test_scores_spread_without_company_ratings():
    leads = [
        {"apollo_person_id": "best", "title": "Product Manager", "company": "A"},
        {"apollo_person_id": "worst", "title": "Accountant", "company": "B"},
    ]
    s = _score(leads, company_fit_scores={})
    best, worst = s["best"]["score"], s["worst"]["score"]
    assert 0 <= worst < best <= 100
    # With the constant 60% blend the gap was capped at 40% of the heuristic
    # gap (about 22 points here); now the heuristic alone decides.
    assert best - worst > 40


def test_a_real_company_rating_still_blends_in():
    leads = [{"apollo_person_id": "x", "title": "Product Manager", "company": "Acme"}]
    unrated = _score(leads)["x"]["score"]
    rated_high = _score(leads, company_fit_scores={"acme": 10})["x"]["score"]
    rated_low = _score(leads, company_fit_scores={"acme": 1})["x"]["score"]
    assert rated_low < unrated < rated_high


def test_lead_found_under_location_filter_scores_location():
    leads = [
        {"apollo_person_id": "local", "title": "Product Manager", "company": "A",
         "in_preferred_location": True},
        {"apollo_person_id": "global", "title": "Product Manager", "company": "A"},
    ]
    s = _score(leads)
    assert s["local"]["_location_score"] == 10
    assert s["global"]["_location_score"] == 0
    assert s["local"]["score"] > s["global"]["score"]


def test_a_known_city_still_wins_over_the_filter_hint():
    leads = [{"apollo_person_id": "x", "title": "PM", "company": "A",
              "location": "London, United Kingdom", "in_preferred_location": True}]
    assert _score(leads)["x"]["_location_score"] == 0


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Candidate.__table__, Lead.__table__])
    session = sessionmaker(bind=engine)()
    session.add(Candidate(id=1, user_id="u1", resume_text="..."))
    session.commit()
    yield session
    session.close()


def _people(prefix, n):
    return [{"id": f"{prefix}{i}", "first_name": "N", "last_name": f"{prefix}{i}",
             "title": "Product Manager", "organization": {"name": f"{prefix}co{i}"}}
            for i in range(n)]


def test_collector_records_leads_found_under_person_locations(db, monkeypatch):
    class _Keys:
        def has_valid_key(self):
            return True
    import services.shared.apollo_key_manager as km
    monkeypatch.setattr(km, "apollo_keys", _Keys())

    # The first location-filtered page finds 2 people and the first global
    # page (a stage that dropped person_locations) finds 2 more; the rest are empty.
    served: set[str] = set()

    def fake_page(payload):
        kind = "loc" if payload.get("person_locations") else "glb"
        if payload.get("page") != 1 or kind in served:
            return []
        served.add(kind)
        return _people(kind, 2)
    monkeypatch.setattr(lc, "_try_collect_page", fake_page)

    filters = LeadFilter(
        target_segments=[TargetSegment(company_size_range="1,10000", person_titles=["Product Manager"])],
        person_locations=["Bengaluru, India"],
    )
    ids: set[int] = set()
    assert lc.collect_leads(filters, 1, 800, db, in_location_ids=ids) == 4
    local = {l.id for l in db.query(Lead).filter(Lead.apollo_id.like("loc%"))}
    assert ids == local and len(local) == 2


def test_discovery_scoring_stores_location_for_leads_found_in_city(db, monkeypatch):
    from api import routes_discovery as rd
    from database.models import LeadScore
    Base.metadata.create_all(db.get_bind(), tables=[LeadScore.__table__])
    monkeypatch.setattr(rd, "JUSTIFY_TOP_K", 0)  # no LLM pass in this test
    cand = db.get(Candidate, 1)
    cand.target_roles = ["Product Manager"]
    db.add_all([Lead(id=1, candidate_id=1, name="a", title="Product Manager", company="A"),
                Lead(id=2, candidate_id=1, name="b", title="Product Manager", company="B")])
    db.commit()
    rd._score_candidate_leads(db, cand, in_location_ids={1})
    loc = {s.lead_id: s.location_relevance for s in db.query(LeadScore)}
    assert loc == {1: 10, 2: 0}
