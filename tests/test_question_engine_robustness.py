"""The quiz must not dead-end because an LLM returned the wrong type.

resume_profile is written by an LLM extraction step, so its field types are a
promise rather than a guarantee: a field documented as a string can arrive as a
number, a dict, or a list. The `or ""` idiom used throughout question_engine
guards None and empty but not a wrong type — (123 or "").lower() raises
AttributeError — and that exception propagates out of the quiz stream endpoint,
which means the student's quiz stops and cannot be continued.

The audit filed this as "real latent defect, not currently reproducible". These
tests make it non-reproducible on purpose rather than by luck.
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services.candidate_intelligence.question_engine import (
    _as_text,
    _as_text_list,
    _coerce_profile,
    build_question_sequence,
)


@pytest.mark.parametrize("value,expected", [
    ("engineering", "engineering"),
    (None, ""),
    (123, "123"),
    (12.5, "12.5"),
    ({"a": 1}, ""),
    (["a", "b"], ""),
    (True, "True"),
])
def test_as_text(value, expected):
    assert _as_text(value) == expected


@pytest.mark.parametrize("value,expected", [
    (["a", "b"], ["a", "b"]),
    ("solo", ["solo"]),
    ("", []),
    (None, []),
    ([1, 2], ["1", "2"]),
    ([None, "a", ""], ["a"]),
    ({"a": 1}, []),
])
def test_as_text_list(value, expected):
    assert _as_text_list(value) == expected


def test_coerce_profile_fixes_wrong_types():
    dirty = {
        "domain": 42,                    # should be a string
        "likely_roles": "Backend Engineer",  # should be a list
        "seniority": {"level": "mid"},   # should be a string
        "top_skills": None,
        "nested_thing": {"keep": "me"},  # not a known field, left alone
    }
    clean = _coerce_profile(dirty)
    assert clean["domain"] == "42"
    assert clean["likely_roles"] == ["Backend Engineer"]
    assert clean["seniority"] == ""
    assert clean["top_skills"] == []
    assert clean["nested_thing"] == {"keep": "me"}


def test_coerce_profile_rejects_a_non_dict():
    assert _coerce_profile(None) == {}
    assert _coerce_profile("not a profile") == {}
    assert _coerce_profile([1, 2]) == {}


@pytest.mark.parametrize("profile", [
    {"domain": 123},
    {"seniority": {"level": "senior"}},
    {"likely_roles": "Backend Engineer"},
    {"top_skills": "Python"},
    {"domain": None, "subdomain": [], "seniority": 0},
    "not a dict at all",
    None,
])
def test_sequence_builds_whatever_the_profile_looks_like(profile):
    """The whole point: no shape of profile should stop the quiz."""
    seq = build_question_sequence({
        "answers": {},
        "resume_profile": profile,
        "resume_text": "Some resume text",
        "parsed_json": {},
    })
    assert isinstance(seq, list) and seq, "quiz produced no questions"
    assert all("key" in q for q in seq)


def test_non_string_answer_does_not_crash():
    """Answers come off the wire, so career_stage is not guaranteed to be text."""
    seq = build_question_sequence({
        "answers": {"career_stage": 5},
        "resume_profile": {},
        "resume_text": "",
        "parsed_json": {},
    })
    assert isinstance(seq, list) and seq
