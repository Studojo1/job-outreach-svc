"""Contact waterfall golden cases: each is a run-36 failure that must now be
structurally impossible."""

import pytest

from services.bob.pipeline.contact import employer_match, rank_people, waterfall
from services.bob.textrails import norm_company

CITY = "Bengaluru"
NO_FETCH = {"read_job": lambda u: (_ for _ in ()).throw(AssertionError("read_job called")),
            "find_people": lambda c, w, ci: (_ for _ in ()).throw(AssertionError("leadsforge called")),
            "web_people": lambda c, ci: (_ for _ in ()).throw(AssertionError("web called"))}


def _opp(company="Sherlock", **kw):
    base = {"company": company, "company_norm": norm_company(company), "role": "AI Intern",
            "evidence_url": "https://www.linkedin.com/posts/x_y-1", "website": "",
            "apply_email": "", "apply_person": "", "author_name": "", "author_headline": "",
            "author_profile": "", "author_affiliation": "unknown"}
    base.update(kw)
    return base


def test_sherlock_email_in_post_beats_any_lookup():
    # priya@sherlock.sh sat in the evidence while a LeadsForge pick shipped.
    d = waterfall(_opp(apply_email="priya@sherlock.sh", apply_person="Priya"),
                  {}, CITY, NO_FETCH, credits_ok=True)
    assert d["contact_source"] == "t0_apply_channel"
    assert d["contact_email"] == "priya@sherlock.sh"
    assert d["contact_tier"] == "T1"


def test_emitrr_recruiter_author_is_the_contact():
    d = waterfall(_opp("Emitrr", apply_email="afreen.naz@emitrr.com",
                       apply_person="Afreen Naz", author_name="Afreen Naz",
                       author_headline="Recruitment Manager at Emitrr",
                       author_affiliation="insider"),
                  {}, CITY, NO_FETCH, credits_ok=True)
    assert d["contact_name"] == "Afreen Naz"
    assert d["contact_title"] == "Recruitment Manager at Emitrr"


def test_cardboard_founder_author_beats_leadsforge():
    d = waterfall(_opp("Cardboard", author_name="Ishan Sharma",
                       author_headline="Founder & CTO at Cardboard",
                       author_affiliation="insider",
                       author_profile="https://www.linkedin.com/in/ishandeveloper"),
                  {}, CITY, NO_FETCH, credits_ok=True)
    assert d["contact_source"] == "t2_insider_author"
    assert d["contact_name"] == "Ishan Sharma"


def test_referral_author_never_becomes_contact_waterfall_continues():
    # Gursimar: "P.S. I'm not the hiring manager" -> aggregator, so LeadsForge runs.
    people = [{"name": "Ravi TA", "title": "Talent Acquisition Lead", "company": "Sarvam AI",
               "city": "Bengaluru"}]
    fetchers = dict(NO_FETCH, find_people=lambda c, w, ci: (people, "people"))
    d = waterfall(_opp("Sarvam", author_name="Gursimar Singh",
                       author_affiliation="aggregator"), {}, CITY, fetchers, True)
    assert d["contact_name"] == "Ravi TA" and d["contact_source"] == "t3_leadsforge"


def test_leadsforge_authority_beats_role_similarity():
    # Stylumia shipped a Data Scientist; the ranker must never pick ICs.
    people = [
        {"name": "Prabal Singh", "title": "Data Scientist", "company": "Stylumia", "city": "Bengaluru"},
        {"name": "Asha K", "title": "Senior Talent Acquisition Specialist", "company": "Stylumia", "city": "Bengaluru"},
        {"name": "Kashvi C", "title": "HR Manager", "company": "JYDigitek", "city": "Delhi"},
    ]
    ranked = rank_people(people, "Stylumia", "https://www.stylumia.ai", CITY)
    assert [r["name"] for r in ranked] == ["Asha K"]  # IC dropped, wrong-employer dropped
    assert ranked[0]["tier"] == "T2"


def test_ic_only_list_ships_blank_not_wrong():
    people = [{"name": "Dev A", "title": "Software Engineer", "company": "Acme", "city": CITY}]
    fetchers = dict(NO_FETCH, find_people=lambda c, w, ci: (people, "people"))
    d = waterfall(_opp("Acme"), {}, CITY, fetchers, credits_ok=False)
    assert d.get("needs_contact") is True and not d.get("contact_name")


def test_collision_repairs_to_next_candidate():
    owners = {"ashak".replace(" ", ""): (norm_company("OtherCo"), "OtherCo")}
    people = [
        {"name": "Asha K", "title": "Talent Acquisition", "company": "Acme", "city": CITY},
        {"name": "Binu R", "title": "HR Executive", "company": "Acme", "city": CITY},
    ]
    fetchers = dict(NO_FETCH, find_people=lambda c, w, ci: (people, "people"))
    d = waterfall(_opp("Acme"), owners, CITY, fetchers, credits_ok=False)
    assert d["contact_name"] == "Binu R"  # repaired, not stripped to blank


def test_employer_match_suffix_roots_and_fuzzy_junk():
    assert employer_match("Honeywell Technologies", "Honeywell")
    assert employer_match("Sherlock AI", "Sherlock")
    assert not employer_match("Cardboard Design Lab", "Cardboard")   # Kashvi-class junk
    assert not employer_match("JYDigitek", "Goodwind")
    assert employer_match("JYDigitek", "SomethingElse", website="https://jydigitek.com")


def test_t4_gated_by_credits():
    d = waterfall(_opp("Tiny"), {}, CITY,
                  dict(NO_FETCH, find_people=lambda c, w, ci: ([], "not_found"),
                       web_people=lambda c, ci: {"name": "W", "title": "Recruiter"}),
                  credits_ok=False)
    assert d.get("needs_contact") is True

    d2 = waterfall(_opp("Tiny"), {}, CITY,
                   dict(NO_FETCH, find_people=lambda c, w, ci: ([], "not_found"),
                        web_people=lambda c, ci: {"name": "W", "title": "Recruiter",
                                                  "profile_url": "https://linkedin.com/in/w"}),
                   credits_ok=True)
    assert d2["contact_source"] == "t4_web" and d2["contact_tier"] == "T3"
