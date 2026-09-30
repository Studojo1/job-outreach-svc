"""B2C audit 30 Sep 2026, UC-Q09 / UC-Q20: loosening stops once enough
relevant leads exist, and founders / C-level are not "broader" matches.

On prod (30 Sep, 4 days of leads) 80% of the 32,886 leads with the scorer's
-25 title penalty were founders or C-level: discovery searches for them on
purpose and they hire for every role, but their titles never contain the
student's role words.
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

from api.routes_candidate import _is_broader  # noqa: E402
from database.models import Base, Candidate, Lead  # noqa: E402
from services.lead_discovery import lead_collector_service as lc  # noqa: E402
from services.lead_scoring.lead_scoring_service import (  # noqa: E402
    is_decision_maker_title, is_relevant_title, role_keywords,
)
from services.shared.schemas.filter_schema import LeadFilter  # noqa: E402
from services.shared.schemas.target_segment_schema import TargetSegment  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Candidate.__table__, Lead.__table__])
    s = sessionmaker(bind=engine)()
    s.add(Candidate(id=1, user_id="u", resume_text="."))
    s.commit()
    yield s
    s.close()


@pytest.mark.parametrize("title,expected", [
    ("Co-Founder & CTO", True), ("Founder and CEO", True), ("C.E.O.", True), ("Chief AI Officer", True),
    ("Managing Director", True), ("President", True), ("Owner", True),
    ("Vice President Sales", False), ("Product Owner", False), ("Founding Engineer", False),
    ("Head of AI", False), ("Accountant", False),
])
def test_decision_maker_titles(title, expected):
    assert is_decision_maker_title(title) is expected


def test_relevant_titles_use_the_scorers_role_words():
    kws = role_keywords(["Software Engineer", "Backend Developer"])
    assert kws == {"software", "engineer", "backend", "developer"}
    assert is_relevant_title("Senior Software Engineering Manager", kws)
    assert is_relevant_title("Co-Founder & CTO", kws)
    assert not is_relevant_title("Sales Associate", kws)


def test_founders_are_strong_matches_other_minus_25_titles_are_broader():
    minus_25 = SimpleNamespace(title_relevance=-25)
    assert _is_broader(minus_25, "Co-Founder & CEO") is False
    assert _is_broader(minus_25, "Sales Associate") is True
    assert _is_broader(SimpleNamespace(title_relevance=30), "Sales Associate") is False
    assert _is_broader(None, "Sales Associate") is False       # unscored is not "broader"


# ── loosening stops once enough relevant leads are stored ─────────────────

def _page(prefix, titles):
    return [{"id": f"{prefix}{i}", "first_name": "N", "last_name": f"{prefix}{i}", "title": t,
             "organization": {"name": f"{prefix}co{i}"}} for i, t in enumerate(titles)]


def _run(db, monkeypatch, role_kws):
    class _Keys:
        def has_valid_key(self):
            return True
    import services.shared.apollo_key_manager as km
    monkeypatch.setattr(km, "apollo_keys", _Keys())
    monkeypatch.setattr(lc, "RELEVANT_LEADS_ENOUGH", 3)
    calls = []

    def fake_page(payload):
        calls.append(payload)
        if payload.get("page") != 1:
            return []
        n = len(calls)
        # Original filters: 3 relevant people. Every looser stage: 2 unrelated.
        if n == 1:
            return _page("orig", ["Product Manager", "Founder & CEO", "Head of Product"])
        return _page(f"loose{n}-", ["Sales Associate", "Accountant"])
    monkeypatch.setattr(lc, "_try_collect_page", fake_page)

    filters = LeadFilter(
        target_segments=[TargetSegment(company_size_range="1,10000", person_titles=["Head of Product"])],
        person_locations=["Bengaluru, India"],
        organization_industries=["Software"],
    )
    return lc.collect_leads(filters, 1, 800, db, role_keywords=role_kws)


def test_loosening_stops_once_enough_relevant_leads_exist(db, monkeypatch):
    assert _run(db, monkeypatch, role_keywords(["Product Manager"])) == 3
    assert db.query(Lead).filter(Lead.apollo_id.like("loose%")).count() == 0


def test_without_role_words_the_ladder_runs_as_before(db, monkeypatch):
    assert _run(db, monkeypatch, None) > 3
    assert db.query(Lead).filter(Lead.apollo_id.like("loose%")).count() > 0
