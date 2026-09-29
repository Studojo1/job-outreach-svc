"""UC-Q36: with leads(candidate_id, apollo_id) unique, the extension must reuse
a discovered lead for the same person instead of failing on insert."""
import pathlib
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api.routes_extension import _insert_or_reuse_lead
from database.models import Base, Candidate, Lead


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Candidate.__table__, Lead.__table__])
    with engine.begin() as c:
        c.execute(text("CREATE UNIQUE INDEX uq_leads_candidate_apollo ON leads (candidate_id, apollo_id) WHERE apollo_id IS NOT NULL"))
    s = sessionmaker(bind=engine)()
    s.add(Candidate(id=1, user_id="u", resume_text="."))
    s.add(Lead(id=10, candidate_id=1, apollo_id="ap1", name="Asha", company="Acme", status="new"))
    s.commit()
    yield s
    s.close()


def test_same_person_reuses_the_discovered_lead(db):
    lead = _insert_or_reuse_lead(db, Lead(candidate_id=1, apollo_id="ap1", name="Asha", company="Acme", status="extension_pending"))
    assert lead.id == 10
    assert db.query(Lead).count() == 1


def test_a_new_person_is_inserted(db):
    lead = _insert_or_reuse_lead(db, Lead(candidate_id=1, apollo_id="ap2", name="Ravi", company="Acme", status="extension_pending"))
    assert lead.id != 10 and db.query(Lead).count() == 2


def test_no_apollo_id_still_inserts(db):
    _insert_or_reuse_lead(db, Lead(candidate_id=1, apollo_id=None, name="X", company="Acme", status="extension_pending"))
    _insert_or_reuse_lead(db, Lead(candidate_id=1, apollo_id=None, name="Y", company="Acme", status="extension_pending"))
    assert db.query(Lead).count() == 3
