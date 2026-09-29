"""B2C audit 29 Sep 2026: security rows HP-N05, PS-N17, PS-N08, PS-N12, PS-N13, PS-N11.

Every test calls the production handler or helper, not a copy.
"""
import asyncio
import pathlib
import sys
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import (  # noqa: E402
    Base, Campaign, Candidate, EmailAccount, Lead, LeadScore, OutreachOrder, User, UserCredit,
)
from api import routes_campaign, routes_orders  # noqa: E402
from api.routes_candidate import get_candidate_leads  # noqa: E402
from services.email_campaign import campaign_service  # noqa: E402


from datetime import datetime  # noqa: E402

_NOW = datetime(2026, 9, 29)


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (
        User, Candidate, Lead, LeadScore, Campaign, EmailAccount, OutreachOrder, UserCredit,
    )])
    s = sessionmaker(bind=engine)()
    s.execute(text("CREATE TABLE test_launch_jobs (job_id TEXT PRIMARY KEY, data JSON, updated_at TIMESTAMP)"))
    s.add_all([
        User(id="me", email="me@gmail.com", name="Me", created_at=_NOW, updated_at=_NOW),
        User(id="them", email="them@gmail.com", name="Them", created_at=_NOW, updated_at=_NOW),
        Candidate(id=1, user_id="me", resume_text=".", parsed_json={"career_analysis": {"x": 1}}),
        Candidate(id=2, user_id="them", resume_text=".", parsed_json={"career_analysis": {"x": 1}}),
        Lead(id=11, candidate_id=1, name="HM", company="Acme", email="hm@acme.com", email_verified=True),
        EmailAccount(id=100, user_id="me", email_address="me.alt@gmail.com", access_token="t"),  # noqa: S106
        EmailAccount(id=200, user_id="them", email_address="them@gmail.com", access_token="t"),  # noqa: S106
        OutreachOrder(id=5, user_id="me", status="leads_ready", candidate_id=1),
    ])
    s.commit()
    yield s
    s.close()


ME = SimpleNamespace(id="me")


# ── PS-N17: no campaign on someone else's candidate ─────────────────────────

def test_resolver_never_passes_a_foreign_candidate_through(db):
    # With a complete candidate of their own, the caller is rebound to it...
    assert routes_campaign._resolve_effective_candidate(db, "me", 2) == 1
    # ...and with none, a foreign id is a 404, not handed back unchanged.
    with pytest.raises(HTTPException) as exc:
        routes_campaign._resolve_effective_candidate(db, "nobody", 2)
    assert exc.value.status_code == 404


def test_resolver_keeps_an_owned_candidate(db):
    assert routes_campaign._resolve_effective_candidate(db, "me", 1) == 1


def test_create_campaign_refuses_a_foreign_candidate(db):
    with pytest.raises(ValueError):
        campaign_service.create_campaign(db, "me", "x", email_account_id=100, candidate_id=2)


def test_create_campaign_refuses_a_foreign_mailbox(db):
    with pytest.raises(ValueError):
        campaign_service.create_campaign(db, "me", "x", email_account_id=200, candidate_id=1)


# ── PS-N08: unpaid users get no campaign setup, no reveals, no emails ───────

def _update(db, status):
    req = routes_orders.OrderUpdateRequest(status=status)
    return asyncio.run(routes_orders.update_order(5, req, current_user=ME, db=db))


def test_unpaid_user_cannot_enter_campaign_setup(db):
    with mock.patch("services.enrichment.enrichment_service.enrich_preview_leads") as reveal:
        with pytest.raises(HTTPException) as exc:
            _update(db, "campaign_setup")
    assert exc.value.status_code == 402
    reveal.assert_not_called()


def test_paid_user_enters_campaign_setup(db):
    db.add(UserCredit(user_id="me", total_credits=200, used_credits=0))
    db.commit()
    with mock.patch("services.enrichment.enrichment_service.enrich_preview_leads"):
        out = _update(db, "campaign_setup")
    assert out["order"]["status"] == "campaign_setup"


def _leads(db):
    request = SimpleNamespace(headers={})
    out = get_candidate_leads(request, 1, limit=None, offset=0, fields=None, current_user=ME, db=db)
    body = out if isinstance(out, dict) else __import__("json").loads(out.body)
    return body["leads"]


def test_leads_api_hides_emails_from_unpaid_users(db):
    assert _leads(db)[0]["email"] is None


def test_leads_api_shows_emails_after_paying(db):
    db.add(UserCredit(user_id="me", total_credits=200, used_credits=200))
    db.commit()
    assert _leads(db)[0]["email"] == "hm@acme.com"


# ── PS-N13: a deliverability test never emails a lead ──────────────────────

def test_test_email_goes_to_own_inbox_not_the_lead(db):
    acct = db.get(EmailAccount, 100)
    assert routes_campaign._test_recipient(db, "me", acct, None) == "me.alt@gmail.com"
    # A lead's (or anyone else's) address as the override is ignored.
    assert routes_campaign._test_recipient(db, "me", acct, "hm@acme.com") == "me.alt@gmail.com"
    assert routes_campaign._test_recipient(db, "me", acct, "them@gmail.com") == "me.alt@gmail.com"
    # The user's own sign-in address is allowed.
    assert routes_campaign._test_recipient(db, "me", acct, "Me@Gmail.com ") == "Me@Gmail.com"


# ── PS-N12: test-launch status is owner-only ────────────────────────────────

def test_test_launch_status_is_owner_only(db):
    db.execute(text("INSERT INTO test_launch_jobs (job_id, data) VALUES ('job1', :d)"),
               {"d": '{"user_id": "me", "status": "done", "leads": []}'})
    db.commit()
    ok = asyncio.run(routes_campaign.test_launch_status("job1", current_user=ME, db=db))
    assert ok["status"] == "done"
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes_campaign.test_launch_status("job1", current_user=SimpleNamespace(id="them"), db=db))
    assert exc.value.status_code == 404


# ── HP-N05 + PS-N11: nothing public that should not be ──────────────────────

def test_public_surface():
    from api.main import app
    paths = {getattr(r, "path", "") for r in app.routes}
    assert not any(p.startswith("/api/v1/marketing") or p.startswith("/api/v1/leadstest") for p in paths)
    assert app.docs_url is None and app.redoc_url is None and app.openapi_url is None
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/metrics").status_code == 401
