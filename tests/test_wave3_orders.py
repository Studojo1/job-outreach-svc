"""Post-payment audit wave 3: one order, many campaigns, nothing orphaned."""
import asyncio
import pathlib
import sys
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import (
    Base, Campaign, Candidate, CreditLedger, EmailAccount, EmailSent, Lead, LeadScore,
    OutreachOrder, User, UserCredit,
)
from services import reconcile


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
        Candidate(id=1, user_id="u", resume_text=".", parsed_json={"career_analysis": {}}),
        EmailAccount(id=5, user_id="u", email_address="u@gmail.com", provider="gmail", access_token="t"),  # noqa: S106
        UserCredit(user_id="u", total_credits=400, used_credits=0),
    ])
    s.commit()
    yield s
    s.close()


class _U:
    id = "u"
    name = "U"


def _create(db, **kw):
    from api.routes_campaign import CampaignCreateRequest, api_create_campaign
    req = CampaignCreateRequest(candidate_id=1, email_account_id=5, name="c", lead_limit=50,
                                selected_styles=["ai"], **kw)
    return asyncio.run(api_create_campaign(req, current_user=_U(), db=db))


def _leads(db, n):
    for i in range(n):
        db.add(Lead(candidate_id=1, name=f"l{i}"))
    db.commit()


def test_second_live_campaign_is_refused_with_409(db):
    _leads(db, 60)
    db.add(Campaign(id=9, candidate_id=1, name="first", status="paused", created_at=NOW))
    db.commit()
    with pytest.raises(HTTPException) as exc:
        _create(db)
    assert exc.value.status_code == 409
    assert exc.value.detail["campaign_id"] == 9
    assert db.query(UserCredit).one().used_credits == 0   # nothing reserved


def test_create_links_the_campaign_to_its_order(db):
    _leads(db, 60)
    db.add(OutreachOrder(id=100, user_id="u", status="campaign_setup", created_at=NOW, action_log=[]))
    db.commit()
    out = _create(db)
    assert db.get(Campaign, out["campaign_id"]).outreach_order_id == 100


def test_launch_true_starts_sending_in_the_same_request(db, monkeypatch):
    from services.email_campaign import campaign_worker
    monkeypatch.setattr(campaign_worker, "compute_campaign_schedule", lambda db_, cid: None)
    _leads(db, 60)
    out = _create(db, launch=True)
    assert db.get(Campaign, out["campaign_id"]).status == "running"


def test_zero_leads_charges_nothing(db):
    with pytest.raises(HTTPException) as exc:
        _create(db)
    assert exc.value.status_code == 400
    assert db.query(UserCredit).one().used_credits == 0
    assert db.query(Campaign).one().status == "cancelled"


def test_stale_draft_is_cancelled_and_its_credits_return(db):
    db.query(UserCredit).update({"used_credits": 50})
    db.add(Campaign(id=7, candidate_id=1, name="d", status="draft", credits_reserved=50,
                    credits_released=0, created_at=NOW - timedelta(hours=2)))
    for _ in range(50):
        db.add(EmailSent(campaign_id=7, status="pending_enrichment", enrichment_status="pending"))
    db.commit()
    reconcile.sweep_stale_drafts(db, NOW)
    assert db.get(Campaign, 7).status == "cancelled"
    assert db.query(UserCredit).one().used_credits == 0


def test_fresh_draft_is_left_alone(db):
    db.add(Campaign(id=7, candidate_id=1, name="d", status="draft", created_at=NOW - timedelta(minutes=10)))
    db.commit()
    assert reconcile.sweep_stale_drafts(db, NOW) == 0


def test_order_whose_campaign_is_gone_goes_back_to_setup(db):
    db.add(OutreachOrder(id=1, user_id="u", status="campaign_running", campaign_id=None, action_log=[]))
    db.add(OutreachOrder(id=2, user_id="u", status="campaign_running", campaign_id=None, action_log=[]))
    db.add(Campaign(id=3, candidate_id=1, name="c", status="running", outreach_order_id=2, created_at=NOW))
    db.commit()
    assert reconcile.reset_orders_without_campaign(db, NOW) == 1
    assert db.get(OutreachOrder, 1).status == "campaign_setup"
    assert db.get(OutreachOrder, 2).status == "campaign_running"  # still has a live campaign


def test_orphan_campaign_is_linked_to_its_order(db):
    db.add(OutreachOrder(id=1, user_id="u", status="campaign_running", campaign_id=4,
                         created_at=NOW - timedelta(days=3), action_log=[]))
    db.add(Campaign(id=3, candidate_id=1, name="orphan", status="running", created_at=NOW - timedelta(days=2)))
    db.add(Campaign(id=4, candidate_id=1, name="pointed", status="paused", created_at=NOW - timedelta(days=1)))
    db.commit()
    reconcile.link_orphan_campaigns(db, NOW)
    assert db.get(Campaign, 3).outreach_order_id == 1
    assert db.get(Campaign, 4).outreach_order_id == 1


def test_reservation_with_no_campaign_is_released_only_when_quiet(db):
    db.query(UserCredit).update({"used_credits": 51})
    db.add(CreditLedger(user_id="u", delta_used=51, reason="opening_balance", created_at=NOW - timedelta(days=5)))
    db.commit()
    assert reconcile.release_orphan_reservations(db, NOW) == 51
    assert db.query(UserCredit).one().used_credits == 0


def test_reservation_in_active_use_is_left_alone(db):
    db.query(UserCredit).update({"used_credits": 51})
    db.add(CreditLedger(user_id="u", delta_used=51, reason="reserve_enrichment", created_at=NOW - timedelta(hours=1)))
    db.commit()
    assert reconcile.release_orphan_reservations(db, NOW) == 0


def test_users_with_campaigns_are_never_touched(db):
    db.query(UserCredit).update({"used_credits": 51})
    db.add(Campaign(id=3, candidate_id=1, name="c", status="completed", created_at=NOW))
    db.commit()
    assert reconcile.release_orphan_reservations(db, NOW) == 0


def test_admin_sees_orphaned_campaigns(db):
    from api.routes_admin import _admin_campaign_row, _campaigns_for_user
    db.add(OutreachOrder(id=1, user_id="u", status="campaign_running", campaign_id=4, action_log=[]))
    db.add(Campaign(id=3, candidate_id=1, name="orphan", status="running", created_at=NOW - timedelta(days=2)))
    db.add(Campaign(id=4, candidate_id=1, name="pointed", status="paused", created_at=NOW - timedelta(days=1)))
    db.add(EmailSent(campaign_id=3, status="sent", sent_at=NOW))
    db.commit()
    rows = {c.id: _admin_campaign_row(db, c, {1}, {4}) for c in _campaigns_for_user(db, "u")}
    assert set(rows) == {3, 4}
    assert rows[3]["orphan"] is True and rows[3]["email_stats"]["sent"] == 1
    assert rows[4]["orphan"] is False
