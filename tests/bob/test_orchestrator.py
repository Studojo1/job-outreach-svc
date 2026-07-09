"""Orchestrator: loop-until-N, widening, and termination are code decisions.
State I/O is stubbed; the loop logic under test is real."""

import pytest

from services.bob.pipeline import orchestrator
from services.bob.pipeline.orchestrator import Budget, PipelineError, run_pipeline


@pytest.fixture(autouse=True)
def quiet_state(monkeypatch):
    monkeypatch.setattr(orchestrator.state, "push_event", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator.state, "save_params", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator.state, "funnel_snapshot",
                        lambda db, run_id: {"raw_items": {}, "opportunities": {}, "vetoes": []})


class FakeDB:
    def rollback(self):
        pass


def _stages(written_per_ring):
    """Fake stage set; assemble yields from written_per_ring per call."""
    calls = {"harvest": 0, "assemble": []}
    seq = iter(written_per_ring)

    def harvest(db, run_id, chat_id, params, budget, ring):
        calls["harvest"] += 1
        return {"ring": ring}

    def assemble(db, run_id, chat_id, params, need):
        calls["assemble"].append(need)
        return {"written": next(seq, 0)}

    noop = lambda db, run_id, chat_id, params, *a: {}
    return {"harvest": harvest, "extract": noop, "verify": noop, "score": noop,
            "contact": lambda db, run_id, chat_id, params, budget: {},
            "assemble": assemble}, calls


PARAMS = {"table_id": 9, "keywords": ["ai intern"], "location": "Bengaluru", "count": 10}


def test_stops_when_target_met_first_ring():
    stages, calls = _stages([10])
    out = run_pipeline(FakeDB(), 1, 1, dict(PARAMS), stages=stages)
    assert out["written"] == 10 and "target met" in out["stopped_because"]
    assert calls["harvest"] == 1  # no pointless widening


def test_widens_and_asks_only_for_the_remainder():
    stages, calls = _stages([6, 4])
    out = run_pipeline(FakeDB(), 1, 1, dict(PARAMS), stages=stages)
    assert out["written"] == 10
    assert calls["harvest"] == 2
    assert calls["assemble"] == [10, 4]  # loop-until-N tracks the deficit


def test_exhausts_rings_and_reports_shortfall_honestly():
    stages, calls = _stages([3, 2, 1])
    out = run_pipeline(FakeDB(), 1, 1, dict(PARAMS), stages=stages)
    assert out["written"] == 6
    assert "sources exhausted" in out["stopped_because"]
    assert "6/10" in out["stopped_because"]


def test_budget_stops_the_loop_not_the_model():
    stages, _ = _stages([2, 2, 2])
    params = dict(PARAMS, credit_cap=5)

    def burning_harvest(db, run_id, chat_id, p, budget, ring):
        budget.spend(5)
        return {}
    stages["harvest"] = burning_harvest
    out = run_pipeline(FakeDB(), 1, 1, params, stages=stages)
    assert "credit cap" in out["stopped_because"]
    assert out["written"] == 2  # first ring completed, second never started


def test_missing_table_or_keywords_refused():
    with pytest.raises(PipelineError):
        run_pipeline(FakeDB(), 1, 1, {"keywords": ["x"], "count": 5})
    with pytest.raises(PipelineError):
        run_pipeline(FakeDB(), 1, 1, {"table_id": 9, "count": 5})


def test_budget_arithmetic():
    b = Budget(credit_cap=10)
    b.spend(4)
    assert b.credits_left == 6 and b.exhausted() == ""
    b.spend(6)
    assert "credit cap" in b.exhausted()
