"""Dream companies must match by name, not by substring.

The scorer called a lead a dream-company match when the answer was a raw
substring of its company name or the other way round, and a match gets +10,
skips every penalty and is floored at 65, so it ranks first and takes the AI
notes. In production (30 days to 10 Oct) 1,893 leads of 65 students matched
only that way: "no" matched every "Technologies" (902 leads, 7 students), "ey"
matched "Bright Money", "cred" matched "Credai Bengaluru", "nvidia" matched a
company called "Vi". The discovery pass also stored every person the fuzzy
organization-name search returned for each answer, "No" and "etc" included.
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
from services.candidate_intelligence.payload_builder import parse_dream_companies, usable_dream_companies
from services.lead_discovery import lead_collector_service as lc
from services.lead_scoring.lead_scoring_service import company_matches_dream, score_and_select_leads
from services.shared.schemas.filter_schema import LeadFilter
from services.shared.schemas.target_segment_schema import TargetSegment


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


# ── which stored answers name a company ─────────────────────────────────────

@pytest.mark.parametrize("stored", [
    ["No"], ["no"], ["etc"], ["Etc"], ["etc."], ["all"], ["ok"], ["Nah"], ["nope"],
    ["nil"], ["No idea"], ["not yet"], ["yes"], ["N/A"], ["-"], ["."], ["x"], ["123"], ["  "],
])
def test_non_answers_are_not_dream_companies(stored):
    assert usable_dream_companies(stored) == []


def test_real_stored_answers_keep_only_the_names():
    # Candidate rows as stored in production.
    assert usable_dream_companies(["Zerodha", "Deloitte", "HSBC", "AMEX", "Etc"]) == [
        "Zerodha", "Deloitte", "HSBC", "AMEX"]
    assert usable_dream_companies(["Sarvam AI", "Krutim", "Eleven Labs", "Super AGI", "etc"]) == [
        "Sarvam AI", "Krutim", "Eleven Labs", "Super AGI"]
    assert usable_dream_companies(["No", "I dont have any preference as such"]) == [
        "I dont have any preference as such"]
    assert usable_dream_companies(["'Duolingo", "salesforce."]) == ["Duolingo", "salesforce"]


def test_two_letter_names_are_kept_and_repeats_dropped():
    assert usable_dream_companies(["EY", "ey", " Google ", "GOOGLE"]) == ["EY", "Google"]


def test_an_entry_is_never_split_again():
    assert usable_dream_companies(["Johnson and Johnson"]) == ["Johnson and Johnson"]


@pytest.mark.parametrize("stored", [None, "Google", {"name": "Google"}, [None, 3, {"name": "Google"}]])
def test_anything_but_a_list_of_strings_names_nothing(stored):
    assert usable_dream_companies(stored) == []


def test_the_quiz_parser_drops_the_same_non_answers():
    assert parse_dream_companies("Zerodha, Deloitte, HSBC, AMEX, Etc") == ["Zerodha", "Deloitte", "HSBC", "AMEX"]
    for answer in ("all", "ok", "etc", "nah", "yes", "nil", "no idea", "not yet"):
        assert parse_dream_companies(answer) == [], answer


# ── does a company name match an answer ─────────────────────────────────────

@pytest.mark.parametrize("company, answer", [
    ("Bright Money", "ey"),
    ("Bentley Systems", "ey"),
    ("Astreya", "ey"),
    ("Credai Bengaluru", "cred"),
    ("Altum Credo Home Finance", "cred"),
    ("Metal Avenues", "meta"),
    ("Deeley Group", "deel"),
    ("Amdahl", "amd"),
    ("Healthify", "ea"),
    ("Cent", "accenture"),
    ("Vi", "nvidia"),
    ("Abc Technologies", "no"),
])
def test_a_substring_is_not_a_match(company, answer):
    assert not company_matches_dream(company, answer)


@pytest.mark.parametrize("company, answer", [
    ("CRED", "cred"),
    ("Meta Platforms", "Meta"),
    ("EY", "EY"),
    ("EY GDS", "EY"),
    ("Google", "Google"),
    ("Google DeepMind", "Google"),
    ("Google", "Google India"),             # the company name inside the answer
    ("Electronic Arts (EA)", "EA"),
    ("Dot & Key", "dot&key"),
    ("Procter and Gamble", "Procter & Gamble"),
    ("L'Oréal", "loreal"),
    ("Masters' Union", "Masters Union"),
])
def test_whole_word_matches(company, answer):
    assert company_matches_dream(company, answer)


@pytest.mark.parametrize("company, answer", [
    ("JPMorgan Chase & Co.", "JP Morgan"),
    ("J.P. Morgan", "JP Morgan"),
    ("JP Morgan Chase", "JPMorgan"),
    ("JPMorgan Chase & Co.", "J P morgan chase"),
    ("PayPal", "Pay Pal"),
])
def test_spacing_inside_a_name_does_not_count(company, answer):
    """Decided: "JP Morgan", "JPMorgan" and "J.P. Morgan" are one name. The
    words may be written with or without spaces, but the match still starts
    and ends on a word boundary, so "JP" alone does not match "JPMorgan"."""
    assert company_matches_dream(company, answer)


def test_spacing_rule_keeps_word_boundaries():
    assert not company_matches_dream("JPMorgan Chase", "JP")
    assert not company_matches_dream("Morgans Hotel Group", "Morgan")


@pytest.mark.parametrize("company, answer", [
    ("", "google"), (None, "google"), ("   ", "google"), ("...", "google"),
    ("Google", ""), ("Google", None), ("", ""),
])
def test_an_empty_name_never_matches(company, answer):
    assert not company_matches_dream(company, answer)


def test_a_one_letter_name_never_matches():
    # "G" and "t" are company names on production leads; "P&G" and "L&T" are
    # real answers, and each holds that letter as a word of its own.
    assert not company_matches_dream("G", "P&G")
    assert not company_matches_dream("t", "L&T")


# ── scoring ─────────────────────────────────────────────────────────────────

PROFILE = {
    "preferred_roles": ["Product Manager"],
    "target_roles": ["Product Manager"],
    "location_preferences": ["Bengaluru"],
    "company_preferences": {},
}
ROLE_INTEL = {"candidate_seniority": "entry", "departments": ["product"]}


def _score(leads, dream):
    out = score_and_select_leads([dict(ld) for ld in leads], PROFILE, ROLE_INTEL,
                                 target_count=len(leads), dream_companies=dream)
    return {ld["apollo_person_id"]: ld for ld in out}


def test_a_no_answer_gives_no_bonus():
    lead = {"apollo_person_id": "x", "title": "Accountant", "company": "Abc Technologies"}
    scored = _score([lead], ["no"])["x"]
    assert scored["_dream_company_score"] == 0
    assert scored["score"] == _score([lead], [])["x"]["score"] < 65


@pytest.mark.parametrize("company, dream", [
    ("Clappia No-Code Platform", ["no"]),
    ("YES SECURITIES", ["Yes"]),
    ("all things people", ["all"]),
])
def test_a_non_answer_that_is_a_word_of_the_name_gives_no_bonus(company, dream):
    # Production lead companies. The word rule alone calls each one a match;
    # only dropping the non-answer keeps the bonus off.
    assert company_matches_dream(company, dream[0])
    lead = {"apollo_person_id": "x", "title": "Accountant", "company": company}
    assert _score([lead], dream)["x"]["_dream_company_score"] == 0


@pytest.mark.parametrize("company, dream", [
    ("Bright Money", ["ey"]),
    ("Credai Bengaluru", ["cred"]),
    ("Metal Avenues", ["Meta"]),
    ("", ["Google"]),
    (None, ["Google"]),
])
def test_substring_and_empty_companies_get_no_bonus(company, dream):
    lead = {"apollo_person_id": "x", "title": "Accountant", "company": company}
    assert _score([lead], dream)["x"]["_dream_company_score"] == 0


def test_a_real_match_keeps_the_bonus_and_the_floor():
    # A title that shares nothing with the target role scores far below 65
    # on its own; at a dream company it is lifted to the floor.
    leads = [
        {"apollo_person_id": "dream", "title": "Accountant", "company": "Google DeepMind"},
        {"apollo_person_id": "other", "title": "Accountant", "company": "Acme"},
    ]
    s = _score(leads, ["No", "Google", "etc"])
    assert s["dream"]["_dream_company_score"] == 10
    assert s["dream"]["score"] >= 65
    assert s["other"]["_dream_company_score"] == 0
    assert s["other"]["score"] < 65


def test_a_real_match_skips_the_penalties():
    profile = {**PROFILE, "company_preferences": {"niche_keywords": ["fintech"]}}
    lead = {"apollo_person_id": "x", "title": "Product Manager", "company": "CRED"}

    def score(dream):
        return score_and_select_leads([dict(lead)], profile, ROLE_INTEL, target_count=1,
                                      dream_companies=dream)[0]["score"]
    # "CRED" carries no "fintech" text, so without the match the -20 niche
    # penalty applies: the match is worth 30 raw points (160 raw = 100).
    assert score(["cred"]) - score(["Credai"]) == pytest.approx(30 * 100 / 160, abs=0.1)


# ── the discovery pass ──────────────────────────────────────────────────────

@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Candidate.__table__, Lead.__table__])
    session = sessionmaker(bind=engine)()
    session.add(Candidate(id=1, user_id="u1", resume_text="..."))
    session.commit()
    yield session
    session.close()


def _person(pid, org):
    return {"id": pid, "first_name": "N", "last_name": pid, "title": "Product Manager",
            "organization": {"name": org}}


def _filters():
    return LeadFilter(
        target_segments=[TargetSegment(company_size_range="1,10000", person_titles=["Product Manager"])],
        person_locations=["Bengaluru, India"],
    )


def test_dream_pass_searches_names_and_keeps_only_people_at_them(db, monkeypatch, caplog):
    searched = []

    def fake_page(payload):
        searched.append(payload.get("q_organization_name"))
        return [
            _person("p1", "CRED"),
            _person("p2", "Credai Bengaluru"),
            _person("p3", "Altum Credo Home Finance"),
            _person("p4", "CRED"),
        ]
    monkeypatch.setattr(lc, "_try_collect_page", fake_page)

    with caplog.at_level("INFO"):
        added = lc.collect_dream_company_leads(_filters(), ["No", "cred", "etc", "ok"], 1, db)

    assert searched == ["cred"]
    assert added == 2
    assert {ld.company for ld in db.query(Lead)} == {"CRED"}
    assert any("dropped 2 of 4" in r.getMessage() for r in caplog.records)


def test_dream_pass_with_only_non_answers_searches_nothing(db, monkeypatch):
    searched = []

    def fake_page(payload):
        searched.append(payload.get("q_organization_name"))
        return [_person("p1", "Abc Technologies")]
    monkeypatch.setattr(lc, "_try_collect_page", fake_page)
    assert lc.collect_dream_company_leads(_filters(), ["No", "etc", "all", "."], 1, db) == 0
    assert searched == [] and db.query(Lead).count() == 0
