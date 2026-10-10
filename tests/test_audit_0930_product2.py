"""B2C audit 30 Sep 2026, round 2.

UC-Q13: the leads response carries one masked sample email for a student who
cannot see addresses yet, from stored data only (never a paid reveal).
UC-Q09: packs much bigger than the pool of strong matches are not offered and
create-order refuses them; the smallest pack always stays sellable.
HP-N13: the server-side Meta Purchase is not sent for an EU/UK buyer who did
not accept tracking; unknown consent from an EU/UK time zone counts as no.

Everything here calls the production functions and routes.
"""
import asyncio
import json
import pathlib
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api import routes_payment  # noqa: E402
from api.dependencies import get_current_user  # noqa: E402
from api.routes_candidate import count_strong_leads, get_candidate_leads, mask_email  # noqa: E402
from core import meta_capi  # noqa: E402
from core.pricing import sellable_email_packs  # noqa: E402
from database.models import (  # noqa: E402
    Base, Campaign, Candidate, CreditLedger, Lead, LeadScore, OutreachOrder, PaymentOrder, User, UserCredit,
)
from database.session import get_db  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


NOW = datetime(2026, 9, 30, 12, 0, 0)
TABLES = (User, Candidate, Lead, LeadScore, UserCredit, Campaign, OutreachOrder, PaymentOrder, CreditLedger)


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[t.__table__ for t in TABLES])
    return sessionmaker(bind=engine)


def _add_leads(s, candidate_id, n_strong, n_broader, start=1, **lead_kw):
    """n_strong leads with a matching title, n_broader with a -25 title."""
    i = start
    for kind, count in (("strong", n_strong), ("broader", n_broader)):
        for _ in range(count):
            s.add(Lead(id=i, candidate_id=candidate_id, name=f"p{i}", company=f"c{i}",
                       title="Sales Associate", apollo_id=f"ap{i}", **lead_kw))
            s.add(LeadScore(lead_id=i, overall_score=90 - i % 50, title_relevance=30 if kind == "strong" else -25,
                            department_relevance=0, industry_relevance=0, seniority_relevance=0,
                            location_relevance=0))
            i += 1
    return i


def _leads(s, candidate_id=1, user="u"):
    resp = get_candidate_leads(SimpleNamespace(headers={}), candidate_id, limit=None, offset=0, fields=None,
                               current_user=SimpleNamespace(id=user), db=s)
    return json.loads(resp.body)


# ── UC-Q09: packs vs the pool ─────────────────────────────────────────────

def test_packs_bigger_than_the_strong_pool_are_not_sellable():
    assert sellable_email_packs(None) == [200, 350, 500]      # unknown pool: no cap
    assert sellable_email_packs(0) == [200]                   # smallest always sellable
    assert sellable_email_packs(120) == [200]
    assert sellable_email_packs(279) == [200]
    assert sellable_email_packs(280) == [200, 350]            # 80% of 350
    assert sellable_email_packs(399) == [200, 350]
    assert sellable_email_packs(400) == [200, 350, 500]
    assert sellable_email_packs(120, test_mode=True) == [200]


def test_leads_response_lists_the_sellable_packs_and_matches_count_strong_leads():
    S = _session_factory()
    s = S()
    s.add(Candidate(id=1, user_id="u", resume_text="..."))
    _add_leads(s, 1, n_strong=290, n_broader=200)
    s.commit()
    body = _leads(s)
    assert body["strong_total"] == 290
    assert body["sellable_email_packs"] == [200, 350]
    assert count_strong_leads(s, 1) == (490, 290)


@pytest.fixture()
def api(monkeypatch):
    S = _session_factory()
    s = S()
    s.add(User(id="u", email="u@x.com", name="U", email_verified=True, created_at=NOW, updated_at=NOW))
    s.add(OutreachOrder(id=1, user_id="u", status="campaign_setup", action_log=[]))
    s.commit()
    s.close()

    from api.main import app

    def _db():
        d = S()
        try:
            yield d
        finally:
            d.close()
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: S().get(User, "u")
    monkeypatch.setattr(routes_payment, "is_india", lambda req: True)
    monkeypatch.setattr(routes_payment, "detect_country", lambda req: "IN")
    monkeypatch.setattr(routes_payment, "capture", lambda *a, **k: None)
    fake_rz = SimpleNamespace(order=SimpleNamespace(create=lambda d: {"id": "order_rz_p2"}))
    monkeypatch.setattr(routes_payment, "_get_razorpay_client", lambda: fake_rz)
    yield TestClient(app), S
    app.dependency_overrides.clear()


def _path():
    from api.main import app
    for r in app.routes:
        if getattr(r, "path", "").endswith("/payment/create-order"):
            return r.path
    raise AssertionError("create-order route not mounted")


def test_create_order_refuses_a_pack_bigger_than_the_pool_and_sells_the_smallest(api):
    client, S = api
    s = S()
    s.add(Candidate(id=1, user_id="u", resume_text="..."))
    _add_leads(s, 1, n_strong=120, n_broader=300)
    s.commit()
    r = client.post(_path(), json={"plan_id": "email_500", "currency": "INR", "candidate_id": 1})
    assert r.status_code == 409, r.text
    assert "120 strong matches" in r.json()["detail"]
    r = client.post(_path(), json={"plan_id": "email_350", "currency": "INR"})  # active candidate fallback
    assert r.status_code == 409, r.text
    r = client.post(_path(), json={"plan_id": "email_200", "currency": "INR", "candidate_id": 1})
    assert r.status_code == 200, r.text


def test_create_order_sells_every_pack_to_a_big_pool(api):
    client, S = api
    s = S()
    s.add(Candidate(id=1, user_id="u", resume_text="..."))
    _add_leads(s, 1, n_strong=450, n_broader=0)
    s.commit()
    assert client.post(_path(), json={"plan_id": "email_500", "currency": "INR"}).status_code == 200


def test_candidate_id_of_another_user_is_ignored(api):
    client, S = api
    s = S()
    s.add(Candidate(id=1, user_id="u", resume_text="..."))
    _add_leads(s, 1, n_strong=100, n_broader=0)
    s.add(Candidate(id=2, user_id="someone-else", resume_text="..."))
    _add_leads(s, 2, n_strong=500, n_broader=0, start=1000)
    s.commit()
    r = client.post(_path(), json={"plan_id": "email_500", "currency": "INR", "candidate_id": 2})
    assert r.status_code == 409, r.text


# ── UC-Q13: a masked sample email, never a reveal ─────────────────────────

def test_mask_email():
    assert mask_email("Jane.Doe@Acme.com") == "J•••@acme.com"
    assert mask_email("nobody") is None
    assert mask_email("") is None
    assert mask_email(None) is None
    assert mask_email("x@localhost") is None


def test_unpaid_user_gets_a_masked_sample_and_no_address():
    S = _session_factory()
    s = S()
    s.add(Candidate(id=1, user_id="u", resume_text="..."))
    _add_leads(s, 1, n_strong=3, n_broader=0)
    s.query(Lead).filter_by(id=2).update({"email": "jane@acme.com", "email_verified": True})
    s.commit()
    body = _leads(s)
    assert body["sample_email"] == {"masked": "j•••@acme.com", "kind": "email", "company": "c2"}
    assert all(lead["email"] is None for lead in body["leads"])
    assert "jane@acme.com" not in json.dumps(body)


def test_sample_uses_an_address_already_known_for_the_same_person():
    S = _session_factory()
    s = S()
    s.add(Candidate(id=1, user_id="u", resume_text="..."))
    _add_leads(s, 1, n_strong=2, n_broader=0)
    s.add(Candidate(id=9, user_id="other", resume_text="..."))
    s.add(Lead(id=500, candidate_id=9, name="p1", apollo_id="ap1", email="raj@beta.io"))
    s.commit()
    body = _leads(s)
    assert body["sample_email"]["masked"] == "r•••@beta.io"
    assert body["sample_email"]["kind"] == "email"


def test_no_known_email_falls_back_to_the_domain_and_never_reveals(monkeypatch):
    import httpx

    def no_network(*a, **k):  # a reveal would call Apollo; nothing may leave
        raise AssertionError("network call while building the sample email")
    monkeypatch.setattr(httpx.Client, "send", no_network)
    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    S = _session_factory()
    s = S()
    s.add(Candidate(id=1, user_id="u", resume_text="..."))
    _add_leads(s, 1, n_strong=2, n_broader=0, company_domain="acme.com")
    s.commit()
    assert _leads(s)["sample_email"] == {"masked": "•••@acme.com", "kind": "domain", "company": "c1"}


def test_paid_user_sees_real_emails_and_no_sample():
    S = _session_factory()
    s = S()
    s.add(Candidate(id=1, user_id="u", resume_text="..."))
    _add_leads(s, 1, n_strong=1, n_broader=0, email="jane@acme.com")
    s.add(UserCredit(user_id="u", total_credits=200, used_credits=0))
    s.commit()
    body = _leads(s)
    assert body["sample_email"] is None
    assert body["leads"][0]["email"] == "jane@acme.com"


# ── HP-N13: server-side Meta events need consent in the EU/UK ─────────────

def test_meta_allowed_matrix():
    # Unknown consent from an EU/UK time zone is a no.
    assert meta_capi.meta_allowed(None, "Europe/London", "IN") is False
    assert meta_capi.meta_allowed(None, "Atlantic/Canary", None) is False
    assert meta_capi.meta_allowed("granted", "Europe/Berlin", "DE") is True
    assert meta_capi.meta_allowed("denied", "Asia/Kolkata", "IN") is False
    assert meta_capi.meta_allowed(None, "Asia/Kolkata", "IN") is True
    # No time zone (older clients): fall back to the IP country; unknown is no.
    assert meta_capi.meta_allowed(None, None, "IN") is True
    assert meta_capi.meta_allowed(None, None, "GB") is False
    assert meta_capi.meta_allowed(None, None, "UNKNOWN") is False
    assert meta_capi.meta_allowed(None, None, None) is False


def _report(S, monkeypatch):
    sent = []

    async def fake_send(**kw):
        sent.append(kw)
        return meta_capi.PurchaseResult("sent", http_status=200)
    monkeypatch.setattr(routes_payment.meta_capi, "is_configured", lambda: True)
    monkeypatch.setattr(routes_payment.meta_capi, "send_purchase", fake_send)
    monkeypatch.setattr(routes_payment, "_record_meta_purchase", lambda order, result: None)
    s = S()
    order = s.query(PaymentOrder).filter_by(razorpay_order_id="order_rz_p2").one()
    asyncio.run(routes_payment._report_purchase_to_meta(s, order))
    return order, sent


@pytest.mark.parametrize("consent", [None, "denied"])
def test_eu_buyer_without_consent_is_not_reported_to_meta(api, monkeypatch, consent):
    client, S = api
    r = client.post(_path(), json={
        "tier": 200, "currency": "INR", "fbp": "fb.1.1.1", "fbc": "fb.1.1.abc",
        "tracking_consent": consent, "time_zone": "Europe/London",
    }, headers={"X-Forwarded-For": "203.0.113.7"})
    assert r.status_code == 200, r.text
    order, sent = _report(S, monkeypatch)
    assert order.meta_fbp == meta_capi.NO_CONSENT_MARK
    assert order.meta_fbc is None and order.client_ip is None and order.client_user_agent is None
    assert sent == []


def test_eu_buyer_who_accepted_is_reported(api, monkeypatch):
    client, S = api
    r = client.post(_path(), json={
        "tier": 200, "currency": "INR", "fbp": "fb.1.1.1",
        "tracking_consent": "granted", "time_zone": "Europe/London",
    })
    assert r.status_code == 200, r.text
    order, sent = _report(S, monkeypatch)
    assert order.meta_fbp == "fb.1.1.1"
    assert len(sent) == 1 and sent[0]["fbp"] == "fb.1.1.1"


def test_indian_buyer_is_reported_without_being_asked(api, monkeypatch):
    client, S = api
    r = client.post(_path(), json={"tier": 200, "currency": "INR", "time_zone": "Asia/Kolkata"})
    assert r.status_code == 200, r.text
    _, sent = _report(S, monkeypatch)
    assert len(sent) == 1
