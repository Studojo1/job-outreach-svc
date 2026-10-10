"""Company research runs a web search, and its answer cites sources inline, so
industry labels were stored as "AdTech ([example.com](https://...))": 4,201
production leads (3,439 created in the last 30 days) and 1,548 company
profiles by 10 Oct 2026, printed as is on lead cards. The citation is now
removed where research output becomes company_profiles.industries and where
a profile's industry is copied onto a lead.
"""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from database.models import Base, Candidate, CompanyProfile, Lead
from services.company_intelligence import llm_company_research as research
from services.company_intelligence.company_enrichment_service import _apply_llm_research
from services.company_intelligence.lead_backfill import fill_lead_from_profile
from services.company_intelligence.llm_company_research import clean_industry
from services.lead_discovery.lead_collector_service import _store_people


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


# Real values from production leads.industry.
REAL = [
    "Accounting software ([en.wikipedia.org](https://en.wikipedia.org/wiki/Tally_Solutions))",
    "AdTech ([builtin.com](https://builtin.com/company/responsiveads))",
    "AdTech ([tracxn.com](https://tracxn.com/d/companies/tatari/__Lmu3fcX2o1EmQf1d-EIdNADA985n9JKJCA_5T-r8l2M))",
    "Advertising & Marketing ([zoominfo.com](https://www.zoominfo.com/c/viral-groww/482281645))",
    "advertising ([raagnaaiads.com](https://raagnaaiads.com/))",
    "Aerospace & Defence ([slntechnologies.com](https://slntechnologies.com/about-us/))",
    "Aesthetic Medicine ([datanyze.com](https://www.datanyze.com/companies/aayna-clinic/370747005))",
]


@pytest.mark.parametrize("raw", REAL)
def test_real_values_keep_only_the_text_before_the_citation(raw):
    assert clean_industry(raw) == raw.split(" ([")[0]


@pytest.mark.parametrize("value", [
    "Information Technology & Services", "Retail", "Other business activities n.e.c.",
    "Contract Research Organization (CRO)", "charity / 501(c)(3)", "E-commerce",
])
def test_clean_values_pass_through_unchanged(value):
    assert clean_industry(value) == value


@pytest.mark.parametrize("raw,expected", [
    # Other shapes found in production.
    ("Automotive components. ([motherson.com](https://www.motherson.com/))", "Automotive components"),
    ("Digital Marketing([chirpin.in](https://chirpin.in/about-us/))", "Digital Marketing"),
    ("Custom Software & IT Services (([thecompanycheck.com](https://www.thecompanycheck.com/company/s/U722)))",
     "Custom Software & IT Services"),
    ("Contract Research Organization (CRO) ([excelya.com](https://www.excelya.com/))",
     "Contract Research Organization (CRO)"),
    ("charity / 501(c)(3) ([taxexemptworld.com](https://www.taxexemptworld.com/organization.asp?tn=2916531))",
     "charity / 501(c)(3)"),
    ("Jewellery ([en.wikipedia.org](https://en.wikipedia.org/wiki/Titan_(company)))", "Jewellery"),
    ("AdTech ([a.com](https://a.com/), [b.com](https://b.com/x))", "AdTech"),
    # The other rules.
    ("[AdTech](https://example.com/x)", "AdTech"),
    ("Media and [publishing](https://example.com/p)", "Media and publishing"),
    ("AdTech https://example.com/x", "AdTech"),
    ("AdTech (www.example.com)", "AdTech"),
    ("  Retail \n  Banking ", "Retail Banking"),
])
def test_citation_shapes(raw, expected):
    assert clean_industry(raw) == expected


@pytest.mark.parametrize("nothing", ["([tradeevo.com](https://tradeevo.com/))", "https://x.com/a", "", "   ", None, 42])
def test_none_when_nothing_is_left(nothing):
    assert clean_industry(nothing) is None


# ── where research output becomes an industry ──────────────────────────────

def _responses_api(industries):
    """The Responses API reply, with the model's JSON as its output text."""
    text = json.dumps({"what_they_build": "Ad platform.", "industries": industries, "domain": "acme.io"})
    body = {"status": "completed", "usage": {},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}
    return SimpleNamespace(ok=True, status_code=200, json=lambda: body)


def test_research_stores_clean_industries_and_keeps_the_list(monkeypatch):
    monkeypatch.setattr(research.requests, "post", lambda *a, **k: _responses_api(
        [REAL[1], "([tradeevo.com](https://tradeevo.com/))", "Retail"]))
    facts = research.research_company("Acme")
    assert facts["industries"] == ["AdTech", "Retail"]
    profile = CompanyProfile(domain="acme.io")
    _apply_llm_research(profile, facts)
    assert profile.industries == ["AdTech", "Retail"]


def test_a_single_string_from_the_model_is_still_a_list():
    assert research._normalise({"industries": REAL[0]}, "Tally", None)["industries"] == ["Accounting software"]


@pytest.mark.parametrize("industries,expected", [
    (["([x.com](https://x.com/))", REAL[3]], "Advertising & Marketing"),
    (REAL[4], "advertising"),
    (["Fintech"], "Fintech"),
])
def test_copy_onto_a_lead_is_clean_even_from_an_old_profile(industries, expected):
    lead = Lead(company="Acme")
    fill_lead_from_profile(lead, CompanyProfile(domain="acme.io", industries=industries))
    assert lead.industry == expected


def test_new_leads_get_a_clean_industry_from_the_cache():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Candidate.__table__, Lead.__table__, CompanyProfile.__table__])
    db = sessionmaker(bind=engine)()
    db.add_all([Candidate(id=1, user_id="u", resume_text="."),
                CompanyProfile(domain="tatari.tv", name="Tatari", industries=[REAL[2]])])
    db.commit()
    person = {"id": "p1", "first_name": "Priya", "last_name": "Rao", "title": "VP Marketing",
              "organization": {"name": "Tatari"}}
    _store_people([person], candidate_id=1, target_leads=10, db=db, leads_collected=0)
    db.commit()
    assert db.query(Lead).one().industry == "AdTech"
