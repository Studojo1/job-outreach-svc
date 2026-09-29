"""B2C UC-Q32: a re-run of discovery reuses the career strategist's answer.

The strategist LLM call took 14-25s, about half the discovery wait, and ran
again on every search even when nothing it reads had changed.
"""
import asyncio
import pathlib
import sys

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import services.candidate_intelligence.career_strategist as cs
import services.lead_calibration.filter_generator_service as fg
from api import routes_discovery as rd
from database.models import Base, Candidate, Lead, LeadScore
from services.lead_discovery import lead_collector_service as lc


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


class _U:
    id = "u"


class _Collected(Exception):
    pass


PARSED = {"preferences": {"locations": ["Bengaluru"], "niche_keywords": ["fintech"]},
          "career_analysis": {"recommended_roles": [{"title": "Product Manager", "seniority": "entry"}]}}


@pytest.fixture()
def calls(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Candidate.__table__, Lead.__table__, LeadScore.__table__])
    s = sessionmaker(bind=engine)()
    for cid in (1, 2):  # 2 is a re-upload of the same resume
        s.add(Candidate(id=cid, user_id="u", resume_text=".", parsed_json=dict(PARSED),
                        resume_profile={"archetype_label": "PM"}))
    s.commit()
    monkeypatch.setattr(rd, "capture", lambda *a, **k: None)
    import services.stage_tracking as st
    monkeypatch.setattr(st, "safe_advance_discovery_status", lambda *a, **k: None)
    llm_calls = []
    monkeypatch.setattr(cs, "run_career_strategist",
                        lambda *a: llm_calls.append(a) or {"title_clusters": [], "keyword_strategy": ["x"]})
    monkeypatch.setattr(fg, "generate_apollo_filters", lambda profile, db, search_strategy=None: object())
    monkeypatch.setattr(lc, "quality_probe_loop", lambda f, prefs, n, cid=None: (f, []))
    # Stop right after filter generation; the strategist has run by then.
    monkeypatch.setattr(rd, "collect_leads", lambda *a, **k: (_ for _ in ()).throw(_Collected()))
    yield s, llm_calls
    s.close()


def _search(db, cid):
    with pytest.raises(HTTPException):  # _Collected surfaces as a 500
        asyncio.run(rd.search_leads(rd.DiscoveryRequest(candidate_id=cid), BackgroundTasks(),
                                    current_user=_U(), db=db))


def test_second_run_does_not_call_the_llm(calls):
    db, llm_calls = calls
    _search(db, 1)
    _search(db, 1)
    assert len(llm_calls) == 1


def test_reupload_with_same_inputs_reuses_the_strategy(calls):
    db, llm_calls = calls
    _search(db, 1)
    _search(db, 2)
    assert len(llm_calls) == 1


def test_changed_preferences_miss_the_cache(calls):
    db, llm_calls = calls
    _search(db, 1)
    c = db.get(Candidate, 1)
    c.parsed_json = {**c.parsed_json, "preferences": {"locations": ["Mumbai"]}}
    db.commit()
    _search(db, 1)
    assert len(llm_calls) == 2
