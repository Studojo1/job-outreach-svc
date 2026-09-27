"""Quiz audit Q21 (decided 2026-09-27): quiz_completed is marked only once the
build has produced target_roles."""
from types import SimpleNamespace

import pytest

import api.routes_candidate as rc


@pytest.fixture
def marks(monkeypatch):
    calls = []
    import services.stage_tracking as st
    monkeypatch.setattr(st, "safe_mark_stage",
                        lambda db, uid, stage, candidate_id=None: calls.append((uid, stage, candidate_id)))
    return calls


@pytest.mark.parametrize("roles", [None, [], [""], ["  "], "Software Engineer", {"a": 1}])
def test_no_usable_roles_means_not_completed(marks, roles):
    cand = SimpleNamespace(id=7, target_roles=roles)
    assert rc._mark_quiz_completed_if_targeted(None, "u1", cand) is False
    assert marks == []


def test_roles_present_marks_completed_once(marks):
    cand = SimpleNamespace(id=7, target_roles=["Software Engineer", "Backend Engineer"])
    assert rc._mark_quiz_completed_if_targeted(None, "u1", cand) is True
    assert marks == [("u1", "quiz_completed", 7)]


def test_stream_completion_no_longer_marks_the_stage():
    import inspect
    src = inspect.getsource(rc)
    stream = src[src.index("# ── Quiz complete"):src.index("# ── Serve next question instantly")]
    assert "safe_mark_stage" not in stream
