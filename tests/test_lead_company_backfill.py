"""UC-Q03 (data half): stored leads carried no industry, company size,
description and, for 91%, no domain, because Apollo's free people search
returns no organization data. They are now filled from the company_profiles
cache when leads are stored and again before they are scored."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from database.models import Base, Candidate, CompanyProfile, Lead
from services.company_intelligence.lead_backfill import (
    backfill_leads_from_cache, company_size_bucket,
)
from services.lead_discovery.lead_collector_service import _store_people


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine, tables=[Candidate.__table__, Lead.__table__, CompanyProfile.__table__])
    s = sessionmaker(bind=engine)()
    s.add(Candidate(id=1, user_id="u", resume_text="."))
    s.add_all([
        CompanyProfile(domain="acme.io", name="Acme", industries=["Fintech", "Payments"],
                       employee_count=120, short_description="Payments APIs for Indian SMBs."),
        CompanyProfile(domain="beta.ai", name="Beta Labs", industries=["AI"], employee_count=8),
        # negative-cache sentinel: keyed by the name lowercased, no data
        CompanyProfile(domain="gamma.co", name="gamma.co"),
    ])
    s.commit()
    yield s
    s.close()


def _person(pid, company, **org):
    return {"id": pid, "first_name": "Priya", "last_name": "Rao", "title": "VP Engineering",
            "organization": {"name": company, **org}}


def test_new_leads_are_filled_from_the_cache_by_name(db):
    _store_people([_person("p1", "Acme")], candidate_id=1, target_leads=10, db=db, leads_collected=0)
    db.commit()
    lead = db.query(Lead).one()
    assert lead.industry == "Fintech"
    assert lead.company_size == "51-200"
    assert lead.company_description == "Payments APIs for Indian SMBs."
    assert lead.company_domain == "acme.io"


def test_domain_lookup_wins_and_apollo_values_are_kept(db):
    # Apollo gave a domain and an industry; the name matches a different row.
    _store_people([_person("p1", "Acme", primary_domain="beta.ai", industry="Robotics")],
                  candidate_id=1, target_leads=10, db=db, leads_collected=0)
    db.commit()
    lead = db.query(Lead).one()
    assert lead.industry == "Robotics"      # Apollo's value is not overwritten
    assert lead.company_size == "1-10"      # from beta.ai, not Acme by name
    assert lead.company_domain == "beta.ai"


def test_a_negative_cache_sentinel_is_not_copied_as_a_domain(db):
    _store_people([_person("p1", "gamma.co")], candidate_id=1, target_leads=10, db=db, leads_collected=0)
    db.commit()
    lead = db.query(Lead).one()
    assert lead.company_domain is None


def test_backfill_before_scoring_fills_existing_leads(db):
    db.add(Lead(candidate_id=1, apollo_id="old", name="A", company="Beta Labs", status="discovered"))
    db.commit()
    leads = db.query(Lead).all()
    assert backfill_leads_from_cache(db, leads) == 1
    db.commit()
    lead = db.query(Lead).one()
    assert (lead.industry, lead.company_size, lead.company_domain) == ("AI", "1-10", "beta.ai")


def test_a_missing_cache_table_never_breaks_lead_storage():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Candidate.__table__, Lead.__table__])
    s = sessionmaker(bind=engine)()
    s.add(Candidate(id=1, user_id="u", resume_text="."))
    s.commit()
    n = _store_people([_person("p1", "Acme")], candidate_id=1, target_leads=10, db=s, leads_collected=0)
    s.commit()
    assert n == 1 and s.query(Lead).count() == 1


@pytest.mark.parametrize("n,band", [(None, None), (0, None), (10, "1-10"), (11, "11-50"),
                                    (5000, "1001-5000"), (20000, "5001-10000")])
def test_size_bands_match_parse_apollo_person(n, band):
    assert company_size_bucket(n) == band


def test_one_off_script_is_a_dry_run_unless_applied(db):
    from scripts.backfill_lead_company_fields import run
    db.add(Lead(candidate_id=1, apollo_id="old", name="A", company="Acme", status="discovered"))
    db.commit()

    assert run(db, apply=False, days=None) == {"scanned": 1, "filled": 1}
    assert db.query(Lead).one().industry is None  # rolled back

    assert run(db, apply=True, days=None, batch=1) == {"scanned": 1, "filled": 1}
    assert db.query(Lead).one().industry == "Fintech"
