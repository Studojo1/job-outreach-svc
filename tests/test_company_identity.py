"""A short company name must not match a different company.

Pranav's Neo posting, from the live pod logs:

    [CONTACT-FIND] Neo resolved to domains:
        ['kotakneo.com', 'neo.gg', 'neofinancial.com']
    [CONTACT-FIND] company=Neo role=Hr Ops Intern found=17
    [EXT-RESOLVE]  outcome=error source=unknown company=Neo

Three unrelated companies — an Indian broking app, a gaming site, a Canadian
fintech — all matched "Neo". We searched all of them, "found" 17 people, and
told the student nobody at Neo could be reached while a real person was on
their screen.

Worse than the empty state: those domains are handed to email_matches_company
as "the strongest evidence there is", so a wrong one does not merely miss — it
ACCEPTS a stranger at a company the student never clicked.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services.extension.contact_finder import (
    _same_company, _normalise_company, email_matches_company,
)


def test_a_prefix_word_means_a_different_company():
    # "Kotak Neo" is Kotak's product, not Neo.
    assert not _same_company("Neo", "Kotak Neo")
    assert not _same_company("Pay", "Google Pay")
    # And the original Bajaj case stays rejected.
    assert not _same_company("Bajaj Finance", "Bajaj Housing Finance")


def test_a_trailing_word_is_usually_the_same_company():
    # These are real cases that must keep working.
    assert _same_company("Sarvam", "Sarvam AI")
    assert _same_company("Razorpay", "Razorpay Software")
    assert _same_company("Pipraiser", "Pipraiser Technologies Pvt Ltd")
    assert _same_company("Ninjacart", "Ninjacart India")


def test_substrings_are_never_matches():
    assert not _same_company("Stripe", "Striped Analytics")
    assert not _same_company("Meta", "Metabase")


class _Resp:
    ok, status_code, text = True, 200, ""
    def __init__(self, orgs): self._o = orgs
    def json(self): return {"organizations": self._o}


def _resolve(monkeypatch_orgs, company="Neo"):
    import services.extension.contact_finder as cf
    import services.shared.apollo_key_manager as akm
    fake = lambda url, json=None, timeout=None, **kw: _Resp(monkeypatch_orgs)
    cf.apollo_post = fake
    akm.apollo_post = fake
    return cf._resolve_company_domains(company)


NEO_ORGS = [
    {"name": "Kotak Neo", "primary_domain": "kotakneo.com"},
    {"name": "Neo.gg", "primary_domain": "neo.gg"},
    {"name": "Neo Financial", "primary_domain": "neofinancial.com"},
]


def test_ambiguity_claims_no_domain():
    """Several equally-good candidates mean we do NOT know which company
    this is. Claiming one anyway is how a student emails the wrong employer."""
    assert _resolve(NEO_ORGS) == []


def test_an_exact_name_match_breaks_the_tie():
    got = _resolve(NEO_ORGS + [{"name": "Neo", "primary_domain": "theneo.co"}])
    assert got == ["theneo.co"]


def test_an_unambiguous_company_still_resolves():
    got = _resolve([{"name": "Razorpay Software", "primary_domain": "razorpay.com"}],
                   company="Razorpay")
    assert got == ["razorpay.com"]


def test_wrong_company_addresses_are_refused_without_a_domain():
    """With no resolved domain the name check must still do its job — this is
    what actually protects the student once we admit we are unsure."""
    assert not email_matches_company("someone@kotakneo.com", "Neo", [])
    assert not email_matches_company("someone@neofinancial.com", "Neo", [])
    assert not email_matches_company("someone@gmail.com", "Neo", [])


def test_the_razorpay_incident_stays_fixed():
    assert not email_matches_company("sumit@razorcapital.net", "Razorpay", ["razorpay.com"])
    assert email_matches_company("x@razorpay.com", "Razorpay", ["razorpay.com"])
