"""A paid people/match reveal also returns the person's LinkedIn URL and
city, state and country. The free search used for discovery returns neither,
so no lead had them. Every path that stores a successful reveal now keeps
them, on blank fields only.

Runs the production enrichment paths against SQLite; only the people/match
call is faked.
"""
import pathlib
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import (  # noqa: E402
    Base, Campaign, Candidate, CreditLedger, EmailAccount, EmailSent, EnrichmentJob, Lead, LeadScore,
    OutreachOrder, SuppressedEmail, User, UserCredit,
)
from services.email_campaign import apollo_frontload, campaign_worker  # noqa: E402
from services.enrichment import enrichment_service as es  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


NOW = datetime(2026, 10, 10, 12, 0, 0)
LI = "http://www.linkedin.com/in/priya-sharma-1a2b3c"
WHERE = "Gurugram, Haryana, India"


def _person(**extra):
    return {"email": "priya@acme.com", "email_status": "verified", "first_name": "Priya",
            "last_name": "Sharma", **extra}


@pytest.fixture()
def apollo(monkeypatch):
    """people/match answers with whatever `person` holds."""
    fake = SimpleNamespace(person=_person(linkedin_url=LI, city="Gurugram", state="Haryana", country="India"))
    monkeypatch.setattr(es.apollo_keys, "has_valid_key", lambda: True)
    monkeypatch.setattr(es, "_record_apollo_reveal", lambda: None)
    monkeypatch.setattr(es, "_is_suppressed_address", lambda a: False)
    monkeypatch.setattr(es, "apollo_post", lambda *a, **k: SimpleNamespace(
        status_code=200, ok=True, json=lambda: {"person": fake.person}))
    monkeypatch.setattr(es.time, "sleep", lambda s: None)
    return fake


@pytest.fixture()
def S(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (
        User, Candidate, Lead, LeadScore, Campaign, EmailAccount, EmailSent, OutreachOrder, EnrichmentJob,
        UserCredit, CreditLedger, SuppressedEmail)])
    factory = sessionmaker(bind=engine)
    s = factory()
    s.add_all([
        User(id="u", email="u@x.com", name="U", email_verified=True, created_at=NOW, updated_at=NOW),
        Candidate(id=1, user_id="u", resume_text="."),
        EmailAccount(id=5, user_id="u", email_address="u@gmail.com", provider="gmail", access_token="t"),  # noqa: S106
        UserCredit(user_id="u", total_credits=10, used_credits=10),
        Campaign(id=10, candidate_id=1, email_account_id=5, name="c", status="running",
                 credits_reserved=10, credits_released=0),
    ])
    s.commit()
    s.close()
    monkeypatch.setattr("database.session.SessionLocal", factory)
    return factory


def _add_lead(S, **kw):
    s = S()
    lead = Lead(candidate_id=1, name="Priya S", company="Acme", **{"apollo_id": "ap1", **kw})
    s.add(lead)
    s.commit()
    lead_id = lead.id
    s.close()
    return lead_id


# ── the reveal result ───────────────────────────────────────────────────────

def test_reveal_returns_linkedin_url_and_location(apollo):
    r = es.enrich_single_lead_classified(Lead(name="Priya S", company="Acme", apollo_id="ap1"))
    assert r.success and r.error_type is None
    assert r.data == {"email": "priya@acme.com", "name": "Priya Sharma", "linkedin_url": LI, "location": WHERE}


@pytest.mark.parametrize("extra,location", [
    ({}, None),
    ({"city": None, "state": "", "country": "India"}, "India"),
    ({"city": "Pune", "state": None, "country": "India"}, "Pune, India"),
    ({"city": 5, "state": ["x"], "country": "India"}, "India"),
])
def test_missing_location_parts_are_fine(apollo, extra, location):
    apollo.person = _person(**extra)
    r = es.enrich_single_lead_classified(Lead(name="Priya S", company="Acme"))
    assert r.success and r.data.get("location") == location
    assert "linkedin_url" not in r.data


@pytest.mark.parametrize("junk", [
    None, "", 12345, ["http://www.linkedin.com/in/x"], "linkedin.com/in/priya",
    "www.linkedin.com/in/priya", "https://twitter.com/priya", "http://www.linkedin.com", "N/A",
])
def test_junk_linkedin_value_is_ignored(apollo, junk):
    apollo.person = _person(linkedin_url=junk)
    r = es.enrich_single_lead_classified(Lead(name="Priya S", company="Acme"))
    assert r.success and r.data["email"] == "priya@acme.com"
    assert "linkedin_url" not in r.data


# ── apply_reveal_fields ─────────────────────────────────────────────────────

def test_fields_are_kept_on_a_blank_lead():
    lead = Lead(apollo_id="ap1")
    es.apply_reveal_fields(lead, {"email": "e@x.com", "linkedin_url": LI, "location": WHERE})
    assert (lead.linkedin_url, lead.location) == (LI, WHERE)


def test_existing_values_are_never_overwritten():
    lead = Lead(apollo_id="ap1", linkedin_url="https://www.linkedin.com/in/own/", location="Delhi, India")
    es.apply_reveal_fields(lead, {"email": "e@x.com", "linkedin_url": LI, "location": WHERE})
    assert (lead.linkedin_url, lead.location) == ("https://www.linkedin.com/in/own/", "Delhi, India")


def test_missing_fields_leave_the_lead_alone():
    lead = Lead(apollo_id="ap1")
    es.apply_reveal_fields(lead, {"email": "e@x.com"})
    es.apply_reveal_fields(lead, None)
    assert (lead.linkedin_url, lead.location) == (None, None)


def test_location_is_cut_to_the_column_width():
    lead = Lead(apollo_id="ap1")
    es.apply_reveal_fields(lead, {"location": "x" * 400})
    assert lead.location == "x" * 255


def test_no_linkedin_url_on_a_lead_without_apollo_id():
    # apollo_id NULL with linkedin_url set is what routes_discovery deletes as
    # a web-discovered lead on the next LinkedIn search.
    lead = Lead(apollo_id=None)
    es.apply_reveal_fields(lead, {"linkedin_url": LI, "location": WHERE})
    assert (lead.linkedin_url, lead.location) == (None, WHERE)


@pytest.mark.parametrize("data", [{"location": 5}, "not a dict", {"linkedin_url": LI, "location": object()}])
def test_never_raises(data):
    es.apply_reveal_fields(Lead(apollo_id="ap1"), data)


# ── every path that stores a reveal ─────────────────────────────────────────

def _via_enrich_contacts(S, lead_id):
    s = S()
    es.enrich_contacts(s, candidate_id=1, limit=1)
    s.close()


def _via_preview(S, lead_id):
    lock = SimpleNamespace(execute=lambda *a, **k: SimpleNamespace(scalar=lambda: True), close=lambda: None)
    with mock.patch("database.session.engine", SimpleNamespace(connect=lambda: lock)):
        es.enrich_preview_leads(candidate_id=1, n=1)


def _via_enrichment_job(S, lead_id):
    import api.routes_enrichment as enr
    with mock.patch.object(enr, "SessionLocal", S), mock.patch.object(enr.time, "sleep"), \
            mock.patch.object(enr, "capture"):
        enr._enrichment_jobs["j"] = {"status": "running", "user_id": "u"}
        enr._run_enrichment_in_background("j", candidate_id=1, limit=1, user_id="u", order_id=None)


def _via_jit_worker(S, lead_id):
    s = S()
    row = EmailSent(campaign_id=10, lead_id=lead_id, status="pending_enrichment", enrichment_status="pending")
    s.add(row)
    s.commit()
    assert campaign_worker._enrich_one(s, row) is True
    assert s.get(EmailSent, row.id).to_email == "priya@acme.com"
    s.close()


def _via_frontload(S, lead_id):
    s = S()
    assert apollo_frontload._enrich_lead(s.get(Lead, lead_id)) == "ok"
    s.commit()
    s.close()


SITES = [_via_enrich_contacts, _via_preview, _via_enrichment_job, _via_jit_worker, _via_frontload]


@pytest.mark.parametrize("site", SITES, ids=lambda f: f.__name__)
def test_every_reveal_path_keeps_the_fields(apollo, S, site):
    lead_id = _add_lead(S)
    site(S, lead_id)
    lead = S().get(Lead, lead_id)
    assert (lead.email, lead.email_verified, lead.status) == ("priya@acme.com", True, "enriched")
    assert (lead.linkedin_url, lead.location) == (LI, WHERE)


@pytest.mark.parametrize("site", SITES, ids=lambda f: f.__name__)
def test_every_reveal_path_keeps_existing_values(apollo, S, site):
    lead_id = _add_lead(S, linkedin_url="https://www.linkedin.com/in/own/", location="Delhi, India")
    site(S, lead_id)
    lead = S().get(Lead, lead_id)
    assert lead.email == "priya@acme.com"
    assert (lead.linkedin_url, lead.location) == ("https://www.linkedin.com/in/own/", "Delhi, India")
