"""B2C UC-Q07: the lead-quality probe checks the filters that will be used.

It probed the original filter set, which returned nobody on 5 of 6 production
runs, so it logged "Empty probe, skipping" and never judged a batch. When it
did judge one poorly, discovery carried on without telling anyone.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import services.lead_calibration.lead_quality_evaluator as evaluator
from services.lead_discovery import lead_collector_service as lc
from services.shared.schemas.filter_schema import LeadFilter
from services.shared.schemas.target_segment_schema import TargetSegment


def _filters():
    return LeadFilter(
        target_segments=[TargetSegment(company_size_range="1,200", person_titles=["Product Manager"])],
        person_locations=["Bengaluru, India"],
        organization_locations=["Bengaluru, India"],
        q_organization_keyword_tags=["fintech"],
    )


def _setup(monkeypatch, score):
    probed = []

    def fake_probe(f):
        probed.append(f)
        # The original filters (with company HQ) match nobody; loosened ones do.
        return [] if f.organization_locations else [{"name": "x", "title": "PM", "company": "c"}]
    monkeypatch.setattr(lc, "_probe_batch", fake_probe)
    evaluated = []
    monkeypatch.setattr(evaluator, "evaluate_probe_with_llm",
                        lambda probe, prefs: evaluated.append(probe) or {"quality_score": score})
    alerts = []
    monkeypatch.setattr(lc, "_alert_low_quality", lambda *a: alerts.append(a))
    return probed, evaluated, alerts


def test_probe_judges_the_first_stage_with_results(monkeypatch):
    probed, evaluated, alerts = _setup(monkeypatch, 8)
    lc.quality_probe_loop(_filters(), {}, 1, candidate_id=7)
    assert len(evaluated) == 1  # it evaluated a real batch instead of skipping
    assert probed[0].organization_locations and probed[1].organization_locations is None
    assert alerts == []


def test_low_score_pages_ops(monkeypatch):
    _, _, alerts = _setup(monkeypatch, 3)
    lc.quality_probe_loop(_filters(), {}, 1, candidate_id=7)
    assert len(alerts) == 1
    candidate_id, score, _issue, stage = alerts[0]
    assert (candidate_id, score) == (7, 3) and stage >= 1


def test_alert_writes_a_system_event(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import database.session as dbs
    from database.models import Base, SystemEvent
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[SystemEvent.__table__])
    monkeypatch.setattr(dbs, "SessionLocal", sessionmaker(bind=engine))
    lc._alert_low_quality(7, 3, "wrong industry", 2)
    ev = dbs.SessionLocal().query(SystemEvent).one()
    assert ev.event_type == "lead_quality_low"
    assert ev.meta["candidate_id"] == 7 and ev.meta["quality_score"] == 3
