"""Lead intake filters: audit #10 (title blocklist), #34 (placeholder rows),
#40 (Apollo's locked-email sentinel)."""
import pathlib
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import Base, Candidate, Lead
from services.lead_scoring.lead_scoring_service import _IRRELEVANT_TITLE_RE
from services.lead_discovery.lead_collector_service import (
    _store_people, _usable_email, parse_apollo_person,
)


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


# ── #10: whole words only ──
@pytest.mark.parametrize("title", [
    "Head of International Marketing", "Internal Communications Director",
    "VP International", "Director of Internet Sales", "Internship Coordinator",
])
def test_legit_titles_are_not_blocked(title):
    assert not _IRRELEVANT_TITLE_RE.search(title.lower())


@pytest.mark.parametrize("title", [
    "Marketing Intern", "Intern - Product", "Freelancer", "Independent Contractor",
])
def test_blocklisted_titles_still_blocked(title):
    assert _IRRELEVANT_TITLE_RE.search(title.lower())


# ── #40: locked-email sentinel ──
@pytest.mark.parametrize("raw,want", [
    ("email_not_unlocked@domain.com", None),
    ("EMAIL_NOT_UNLOCKED@x.io", None),
    ("not-an-email", None),
    ("", None),
    (None, None),
    ("priya@acme.com", "priya@acme.com"),
])
def test_usable_email(raw, want):
    assert _usable_email(raw) == want


# ── #34: no "Unknown ..." placeholders ──
def test_missing_fields_are_none_not_placeholders():
    p = parse_apollo_person({"id": "a1", "first_name": "", "last_name": "", "organization": {}})
    assert p["name"] is None and p["company"] is None and p["title"] is None


def test_obfuscated_last_name_keeps_first_name():
    p = parse_apollo_person({"id": "a1", "first_name": "Priya", "last_name_obfuscated": "S***a",
                             "organization": {"name": "Acme"}})
    assert p["name"] == "Priya"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Candidate.__table__, Lead.__table__])
    s = sessionmaker(bind=engine)()
    s.add(Candidate(id=1, user_id="u", resume_text="."))
    s.commit()
    yield s
    s.close()


def test_store_skips_unidentifiable_people_and_sentinel_emails(db):
    people = [
        {"id": "p1", "first_name": "Priya", "last_name": "Rao", "title": "VP Eng",
         "organization": {"name": "Acme"}, "email": "email_not_unlocked@acme.com"},
        {"id": "p2", "first_name": "", "last_name": "", "organization": {"name": "Beta"}},   # no name
        {"id": "p3", "first_name": "Ravi", "last_name": "K", "organization": {}},            # no company
        {"id": "p4", "first_name": "Asha", "last_name": "M", "organization": {"name": "Gamma"}},  # no title: kept
    ]
    n = _store_people(people, candidate_id=1, target_leads=10, db=db, leads_collected=0)
    db.commit()
    rows = {l.apollo_id: l for l in db.query(Lead).all()}
    assert n == 2 and set(rows) == {"p1", "p4"}
    assert rows["p1"].email is None and rows["p1"].email_verified is False
    assert rows["p4"].title is None
    assert not any((l.name or "").startswith("Unknown") or (l.company or "").startswith("Unknown") for l in rows.values())
