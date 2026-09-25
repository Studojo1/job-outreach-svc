"""Turning a student's typed answer into values, without inventing any.

Two parsers, one shared failure: both used to split on "," and trust whatever
came out.

_parse_multi handles multi-select MCQ answers. The frontend joins the chosen
option texts with ", ", and six of the live options contain a comma of their
own ("Student, not graduating soon"), so a naive split tore one answer into two
values that match nothing downstream.

parse_dream_companies handles a free-text question. 19.6% of candidates had
prose stored as company names, because "I'm not sure, maybe Google" was split
and both halves kept. Those names go to lead discovery, so a bad parse does not
just store junk: it decides who the student gets emailed to. Storing nothing is
the better failure, and most of these tests are about refusing rather than
extracting.
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services.candidate_intelligence.payload_builder import (
    _parse_multi,
    parse_dream_companies,
)


# ── _parse_multi ────────────────────────────────────────────────────────────

def test_option_containing_a_comma_survives_when_options_are_known():
    """Passing the option list is the only unambiguous way to split this.

    "Student, not graduating soon" is genuinely ambiguous on its own: ", " is
    both the separator the frontend joins with and punctuation inside the
    option, so one answer and two answers look identical. With the option list
    the real text is recovered; without it, the caller gets the two-value split
    and that is the honest result rather than a guess.

    In practice the every call site is a multi-select whose options carry no
    commas (location, niche keywords, tech stack), and the comma-carrying
    options all belong to single-select questions that never come through here.
    """
    opts = ["Student, not graduating soon", "Recent graduate (0-2 years exp.)"]
    assert _parse_multi("Student, not graduating soon", opts) == ["Student, not graduating soon"]


def test_two_options_each_containing_a_comma():
    answer = "Student, not graduating soon, I know exactly, give me precise controls"
    options = [
        "Student, not graduating soon",
        "I know exactly, give me precise controls",
    ]
    assert _parse_multi(answer, options) == options


def test_plain_multi_select_still_splits():
    assert _parse_multi("Fintech / Payments, SaaS / B2B") == ["Fintech / Payments", "SaaS / B2B"]


def test_empty_is_empty():
    assert _parse_multi("") == []
    assert _parse_multi("   ") == []


def test_known_options_are_matched_in_answer_order():
    options = ["Python", "Go", "TypeScript"]
    assert _parse_multi("TypeScript, Python", options) == ["TypeScript", "Python"]


# ── parse_dream_companies ───────────────────────────────────────────────────

def test_plain_company_list():
    assert parse_dream_companies("Google, Stripe, Figma") == ["Google", "Stripe", "Figma"]


def test_prose_around_a_real_name_drops_the_prose():
    """The exact shape that filled the column with sentence fragments."""
    assert parse_dream_companies("I'm not sure, maybe Google") == []


def test_a_sentence_is_not_a_company():
    assert parse_dream_companies(
        "honestly I would love to work at a place that treats people well"
    ) == []


@pytest.mark.parametrize("answer", ["skip", "none", "N/A", "idk", "no preference", "-", "  "])
def test_non_answers_store_nothing(answer):
    assert parse_dream_companies(answer) == []


def test_and_separated_names():
    assert parse_dream_companies("Zerodha and Razorpay") == ["Zerodha", "Razorpay"]


def test_long_real_company_names_are_kept():
    assert parse_dream_companies("Tata Consultancy Services, JP Morgan Chase") == [
        "Tata Consultancy Services",
        "JP Morgan Chase",
    ]


def test_duplicates_collapse():
    assert parse_dream_companies("Google, Google, Stripe") == ["Google", "Stripe"]


def test_cap_is_respected():
    assert len(parse_dream_companies(", ".join(f"Co{i}" for i in range(30)))) == 10


def test_numbers_alone_are_not_companies():
    assert parse_dream_companies("123, ...") == []


def test_newline_separated_list():
    assert parse_dream_companies("Google\nStripe\nFigma") == ["Google", "Stripe", "Figma"]


# ── work_mode: the mapping must survive a copy edit ─────────────────────────

def test_every_live_work_mode_option_maps_to_its_declared_value():
    """The mapping is derived from the option definitions, so it cannot drift.

    This is the regression that mattered: "Fully in-office" mapped to
    "flexible", which switches the location filter off in lead discovery, so
    students who asked for office work were shown remote-friendly leads
    anywhere. The old mapping substring-matched the wording and the test read
    "in office" while the copy is hyphenated.
    """
    from services.candidate_intelligence.payload_builder import _map_work_mode
    from services.candidate_intelligence.question_engine import _Q8_WORK_MODE

    for opt in _Q8_WORK_MODE["mcq"]["options"]:
        assert _map_work_mode(opt["text"]) == opt["value"], opt["text"]


def test_a_copy_edit_cannot_break_the_mapping():
    """Rewording an option must not change what it maps to."""
    from services.candidate_intelligence import question_engine as qe
    from services.candidate_intelligence.payload_builder import _map_work_mode

    original = qe._Q8_WORK_MODE["mcq"]["options"]
    edited = [dict(o) for o in original]
    edited[2]["text"] = "On-site, five days a week"  # the onsite option, reworded
    qe._Q8_WORK_MODE["mcq"]["options"] = edited
    try:
        assert _map_work_mode("On-site, five days a week") == "onsite"
    finally:
        qe._Q8_WORK_MODE["mcq"]["options"] = original


def test_canonical_values_pass_through():
    from services.candidate_intelligence.payload_builder import _map_work_mode
    for v in ("remote", "hybrid", "onsite", "flexible"):
        assert _map_work_mode(v) == v


def test_niche_options_are_passed_so_a_comma_edit_cannot_split_one_answer():
    """The known_options path must be live, not dead.

    None of the niche options contains a comma today, so the ", " split
    happens to give the right answer. That is a property of the current copy,
    not of the code. build_payload_from_answers now passes the real option
    list, so an edit that adds a comma to an option cannot quietly start
    tearing one answer into two.
    """
    from services.candidate_intelligence.payload_builder import _parse_multi

    options = ["Fintech / Payments", "Logistics, warehousing and supply chain", "AI / ML"]
    answer = "Logistics, warehousing and supply chain, AI / ML"
    assert _parse_multi(answer, options) == [
        "Logistics, warehousing and supply chain",
        "AI / ML",
    ]
    # Without the options the heuristic still recovers it, because
    # "warehousing and supply chain" reads as a continuation rather than an
    # option label. Passing the options makes it certain rather than likely.
    assert _parse_multi(answer) == [
        "Logistics, warehousing and supply chain",
        "AI / ML",
    ]


# ── typed free text containing a comma ──────────────────────────────────────

def test_typed_prose_with_a_comma_stays_one_answer():
    """The "Other" box is free text, and students use commas in it.

    "I want fintech, healthtech" was split into two pseudo-answers, and both
    went to lead discovery as separate interests.
    """
    assert _parse_multi("I want fintech, healthtech roles") == [
        "I want fintech, healthtech roles"
    ]


def test_ordinary_option_labels_still_split():
    """The common case must not regress: short labels are separate answers."""
    assert _parse_multi("Fintech / Payments, SaaS / B2B, AI / ML") == [
        "Fintech / Payments",
        "SaaS / B2B",
        "AI / ML",
    ]


def test_a_long_clause_is_treated_as_a_continuation():
    assert _parse_multi(
        "Remote, anywhere in the country as long as the team is distributed"
    ) == ["Remote, anywhere in the country as long as the team is distributed"]


def test_two_capitalised_labels_are_not_merged():
    assert _parse_multi("Bengaluru, Mumbai") == ["Bengaluru", "Mumbai"]
