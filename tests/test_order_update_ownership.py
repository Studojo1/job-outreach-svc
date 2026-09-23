"""POST /orders/{id}/update may only bind ids the caller owns.

The order itself was owner-scoped, but candidate_id / campaign_id /
email_account_id from the body were written verbatim, and a foreign
candidate_id plus status=campaign_setup started paid preview enrichment on a
stranger's leads.
"""
import pathlib
import sys

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import Base, Campaign, Candidate, EmailAccount
from api.routes_orders import OrderUpdateRequest, _require_owned_refs


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine, tables=[t.__table__ for t in (Candidate, Campaign, EmailAccount)]
    )
    session = sessionmaker(bind=engine)()
    session.add_all([
        Candidate(id=1, user_id="me", resume_text="."),
        Candidate(id=2, user_id="them", resume_text="."),
        Campaign(id=10, candidate_id=1, name="mine"),
        Campaign(id=20, candidate_id=2, name="theirs"),
        EmailAccount(id=100, user_id="me", email_address="me@x", access_token="t"),
        EmailAccount(id=200, user_id="them", email_address="them@x", access_token="t"),
    ])
    session.commit()
    yield session
    session.close()


def test_own_ids_pass(db):
    _require_owned_refs(db, "me", OrderUpdateRequest(
        candidate_id=1, campaign_id=10, email_account_id=100))


@pytest.mark.parametrize("field,value", [
    ("candidate_id", 2), ("campaign_id", 20), ("email_account_id", 200),
    ("candidate_id", 999),
])
def test_foreign_or_missing_ids_404(db, field, value):
    with pytest.raises(HTTPException) as exc:
        _require_owned_refs(db, "me", OrderUpdateRequest(**{field: value}))
    assert exc.value.status_code == 404


def test_status_only_update_needs_no_lookup(db):
    _require_owned_refs(db, "me", OrderUpdateRequest(status="campaign_setup"))
