"""Seniority fit must read job titles as words, not substrings.

"cto" is inside "Director", "Contractor" and "Factory"; "coo" is inside
"Coordinator"; "vp" is inside "NordVPN". Matched as substrings, a Marketing
Coordinator was scored as a COO: 1/10 for a student (too senior) instead of
the 4 an ordinary title gets. Found while clearing lint on 2026-09-28; a
comparison over 20,000 production titles showed only such misreads change.
"""
import pytest

from services.lead_scoring.lead_scoring_service import _score_seniority_fit as fit


@pytest.mark.parametrize("title", [
    "Marketing Coordinator", "Event Coordinator", "Contractor Support Specialist",
    "Head of SEO @NordVPN",
])
def test_substrings_of_other_words_are_not_c_level(title):
    # An ordinary title for an entry-level candidate scores the default 4
    # (or its real role), never the 1 reserved for C-level.
    assert fit(title, "entry") != 1


@pytest.mark.parametrize("title,expected", [
    ("CTO", 1), ("Co-founder & CEO", 1), ("Chief Operating Officer", 1), ("COO", 1),
])
def test_real_c_level_still_reads_as_c_level_for_students(title, expected):
    assert fit(title, "entry") == expected


@pytest.mark.parametrize("title", ["VP Sales", "SVP Engineering", "EVP, Growth", "AVP - Business Development", "Vice President, Product"])
def test_vp_variants_are_vps(title):
    assert fit(title, "senior") == 10


def test_directors_are_not_ctos_outside_seed_companies():
    # Entry candidate at a mid-size company: a Director scores as a Director.
    assert fit("Director of Marketing", "entry", "201-500") == 4


def test_seed_company_leaders_including_directors_are_hiring_managers():
    for title in ("Founder", "CTO", "VP Engineering", "Director of Growth"):
        assert fit(title, "entry", "1-10") == 10


def test_managers_remain_ideal_for_entry_candidates():
    assert fit("Engineering Manager", "entry", "51-200") == 10
