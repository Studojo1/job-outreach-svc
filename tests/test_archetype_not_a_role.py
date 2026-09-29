"""OP-N09 (29 Sep B2C audit): the quiz archetype is a description, not a role.

The resume prompt asks for an archetype_label that is "NOT a job title from a
job board". It was inserted as quiz option A, 51% of recent students picked
it, and it went to Apollo as q_organization_job_titles, a title nobody holds.
"""
import pytest

from services.candidate_intelligence.career_ontology import (
    nearest_real_title, to_real_titles,
)
from services.candidate_intelligence.question_engine import _build_target_role_question
from services.lead_calibration import filter_generator_service as fg
from services.shared.schemas.candidate_schema import CandidateProfile

ARCHETYPE = "Zero-to-One Growth Systems Builder"


def _question(likely_roles):
    profile = {"archetype_label": ARCHETYPE, "likely_roles": likely_roles, "domain": "marketing"}
    return _build_target_role_question(profile, {})


def _option_texts(q):
    return [o["text"] for o in q["mcq"]["options"]]


def test_archetype_is_not_an_option_but_is_described_above_them():
    q = _question(["Growth Marketing Manager", "Performance Marketer"])
    assert ARCHETYPE not in _option_texts(q)
    assert q["message"].startswith(f"Your profile reads as a {ARCHETYPE}.")
    assert _option_texts(q)[0] == "Growth Marketing Manager"


def test_a_coined_likely_role_is_offered_as_its_nearest_real_title():
    q = _question(["High-Volume Outbound SDR & Pipeline Hygienist", "Growth Marketing Manager"])
    texts = _option_texts(q)
    assert "High-Volume Outbound SDR & Pipeline Hygienist" not in texts
    assert "SDR" in texts


def test_a_coined_likely_role_with_nothing_close_is_left_out():
    q = _question(["Consulting-to-Startup Generalist", "Growth Marketing Manager"])
    assert "Consulting-to-Startup Generalist" not in _option_texts(q)


def test_real_titles_outside_the_ontology_are_still_offered():
    q = _question(["Product Manager", "Marketing Intern"])
    assert {"Product Manager", "Marketing Intern"} <= set(_option_texts(q))


@pytest.mark.parametrize("coined,real", [
    ("Zero-to-One Growth Systems Builder", "Growth Analyst"),
    ("High-Volume Outbound SDR & Pipeline Hygienist", "SDR"),
    ("Applied AI Solutions Architect", "Applied AI Engineer"),
    ("AI-Native Founder's Office Hire", "Founder's Office"),
])
def test_nearest_real_title(coined, real):
    assert nearest_real_title(coined) == real


def test_real_titles_are_kept_as_written():
    roles = ["Software Engineer Intern", "Senior Data Analyst", "Marketing Intern",
             "Analytics Engineer / Power BI Developer", "Pilot"]
    assert to_real_titles(roles) == roles


def _profile(roles):
    return CandidateProfile(
        user_id="u", name="Priya", location_preferences=["Bangalore"], skills=["sql"],
        experience_level="student", preferred_roles=roles, role_seniority_target=["manager"],
        company_preferences={}, work_preferences={},
    )


def test_apollo_hiring_titles_use_real_titles_llm_path():
    f = fg._generate_filters_from_strategy(
        {"person_seniorities": ["manager"]}, _profile([ARCHETYPE, "Growth Marketing Manager"]))
    assert f.q_organization_job_titles == ["Growth Analyst", "Growth Marketing Manager"]


def test_apollo_hiring_titles_use_real_titles_rules_path():
    f = fg._generate_filters_rules_based(_profile([ARCHETYPE, "Growth Analyst"]))
    assert f.q_organization_job_titles == ["Growth Analyst"]
