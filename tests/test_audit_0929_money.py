"""B2C audit 29 Sep 2026: money and routing rows.

PS-N05/PS-N07 (campaign sized to the credits available), PP-P32 (no refund
of a reservation a created campaign holds), OP-N03 (unpaid users with leads
go to them), PS-N02 (running/paused campaign preferred), OP-N04 (no
reference prices), NEW-09 (LinkedIn plans not sold), PP-P45 (one
replacement per source), ST-N04 (reconciler payments reported).

Every test calls production code.
"""
import asyncio
import pathlib
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import (  # noqa: E402
    Base, Campaign, Candidate, CreditLedger, EmailAccount, EmailSent, Lead, LeadScore,
    OutreachOrder, PaymentOrder, User, UserCredit,
)


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


NOW = datetime(2026, 9, 29, 12, 0, 0)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (
        User, Candidate, Lead, LeadScore, Campaign, EmailAccount, EmailSent, OutreachOrder,
        UserCredit, CreditLedger)])
    s = sessionmaker(bind=engine)()
    s.add_all([
        User(id="u", email="u@x.com", name="U", email_verified=True, created_at=NOW, updated_at=NOW),
        Candidate(id=1, user_id="u", resume_text=".", parsed_json={"career_analysis": {}}, created_at=NOW),
        EmailAccount(id=5, user_id="u", email_address="u@gmail.com", provider="gmail", access_token="t"),  # noqa: S106
    ])
    s.commit()
    yield s
    s.close()


class _U:
    id = "u"
    name = "U"


def _wallet(db, total, used=0):
    db.add(UserCredit(user_id="u", total_credits=total, used_credits=used))
    db.commit()


def _leads(db, n, candidate_id=1):
    for i in range(n):
        db.add(Lead(candidate_id=candidate_id, name=f"l{i}"))
    db.commit()


def _create(db, lead_limit):
    from api.routes_campaign import CampaignCreateRequest, api_create_campaign
    req = CampaignCreateRequest(candidate_id=1, email_account_id=5, name="c", lead_limit=lead_limit,
                                selected_styles=["ai"])
    return asyncio.run(api_create_campaign(req, current_user=_U(), db=db))


# ── PS-N05 / PS-N07 ─────────────────────────────────────────────────────────

def test_campaign_uses_every_available_credit_not_the_browser_tier(db):
    _wallet(db, 500)
    _leads(db, 700)
    out = _create(db, lead_limit=200)          # stale localStorage tier
    assert db.get(Campaign, out["campaign_id"]).credits_reserved == 500
    assert db.query(UserCredit).one().used_credits == 500


def test_leftover_credits_join_the_next_campaign(db):
    _wallet(db, 237, used=0)                    # 200 bought + 37 refunded earlier
    _leads(db, 300)
    out = _create(db, lead_limit=200)
    assert db.get(Campaign, out["campaign_id"]).credits_reserved == 237


def test_campaign_is_capped_at_the_lead_count(db):
    _wallet(db, 500)
    _leads(db, 120)
    out = _create(db, lead_limit=500)
    assert db.get(Campaign, out["campaign_id"]).credits_reserved == 120
    assert db.query(UserCredit).one().used_credits == 120


# ── PP-P32 ──────────────────────────────────────────────────────────────────

def test_failure_after_create_does_not_refund_the_campaigns_credits(db, monkeypatch):
    from services import credits
    _wallet(db, 200)
    _leads(db, 200)

    def boom(*a, **k):
        raise RuntimeError("attach failed")
    monkeypatch.setattr(credits, "attach_campaign", boom)
    try:
        _create(db, lead_limit=200)
    except Exception:  # noqa: BLE001 - either outcome is fine; the wallet is what matters
        pass
    campaign = db.query(Campaign).one()
    rows = db.query(EmailSent).filter_by(campaign_id=campaign.id).count()
    # The rows exist, so the credits must still be reserved for them.
    assert rows > 0
    assert db.query(UserCredit).one().used_credits == 200


# ── OP-N03 ──────────────────────────────────────────────────────────────────

def test_unpaid_user_with_leads_is_sent_to_them(db):
    from services.next_step import resolve_next_step
    db.add(Candidate(id=2, user_id="u", resume_text=".", created_at=NOW + timedelta(hours=1)))  # new, no leads
    db.add(OutreachOrder(id=9, user_id="u", status="leads_ready", candidate_id=2, created_at=NOW, action_log=[]))
    db.commit()
    _leads(db, 30, candidate_id=1)
    step = resolve_next_step(db, "u", heal=False)
    assert step.state == "not_paid"
    assert step.path == "/leads/results"
    assert step.candidate_id == 1


def test_unpaid_user_without_leads_gets_no_path(db):
    from services.next_step import resolve_next_step
    step = resolve_next_step(db, "u", heal=False)
    assert step.state == "not_paid" and step.path is None


# ── PS-N02 ──────────────────────────────────────────────────────────────────

def test_running_campaign_beats_a_newer_finished_one(db):
    from services.order_links import current_campaign_id
    from api.routes_campaign import get_user_latest_campaign
    order = OutreachOrder(id=9, user_id="u", status="campaign_running", created_at=NOW, action_log=[])
    db.add(order)
    db.add(Campaign(id=133, candidate_id=1, name="older", status="running", outreach_order_id=9,
                    created_at=NOW - timedelta(days=5)))
    db.add(Campaign(id=134, candidate_id=1, name="newer", status="completed", outreach_order_id=9,
                    created_at=NOW))
    db.commit()
    assert current_campaign_id(db, order) == 133
    latest = asyncio.run(get_user_latest_campaign(current_user=_U(), db=db))
    assert latest["campaign"]["id"] == 133


# ── OP-N04 / NEW-09 ─────────────────────────────────────────────────────────

def test_pricing_has_no_reference_prices_and_no_linkedin_plans(monkeypatch):
    from api import routes_payment
    monkeypatch.setattr(routes_payment, "is_india", lambda req: True)
    out = asyncio.run(routes_payment.get_pricing(SimpleNamespace(headers={})))
    assert all("anchor_display" not in t and "discount_pct" not in t for t in out["tiers"])
    assert {p["plan_type"] for p in out["plans"]} == {"email"}


# ── PP-P45 ──────────────────────────────────────────────────────────────────

def test_a_source_email_gets_one_replacement_only(db):
    from services.email_campaign.replenishment import add_replacement_lead
    _leads(db, 50)
    db.add(Campaign(id=7, candidate_id=1, name="c", status="running", created_at=NOW, credits_reserved=40))
    for lead_id in range(1, 41):
        db.add(EmailSent(campaign_id=7, lead_id=lead_id, status="queued", followup_number=0))
    db.commit()
    src = db.query(EmailSent).filter_by(lead_id=1).one()
    src.status = "bounced"
    db.commit()
    first = add_replacement_lead(db, 7, src.id, "bounce")
    assert first is not None
    assert add_replacement_lead(db, 7, src.id, "bounce") is None


# ── ST-N04 ──────────────────────────────────────────────────────────────────

def test_reconciler_reports_the_payment(monkeypatch):
    from services import payment_reconciler
    from api import routes_payment
    captured, meta = [], []
    monkeypatch.setattr(payment_reconciler, "capture", lambda ev, uid, props: captured.append((ev, props)))

    async def fake_meta(db, order):
        meta.append(order.id)
    monkeypatch.setattr(routes_payment, "_report_purchase_to_meta", fake_meta)
    order = PaymentOrder(id=605, user_id="u", plan_id="email_200", credits_granted=200,
                         amount_cents=182500, currency="INR", tier=200)
    payment_reconciler._report_recovered_payment(None, order, "dodo")
    assert captured and captured[0][0] == "payment_confirmed"
    assert captured[0][1]["trigger"] == "reconciler"  # "source" is overwritten with "server" by capture()
    assert meta == [605]
