"""The enrichment worker may only move the caller's own order (audit #7).

order_id arrives in the request body. Before the fix the worker loaded and
rewrote any order by id, so an attacker enriching their own candidate could
flip a stranger's order to enriching / enrichment_complete and overwrite its
leads_collected.
"""
import pathlib
import sys
from unittest import mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import Base, Candidate, Lead, OutreachOrder

# routes_enrichment imports the payment module for credit refunds, which pulls
# in the payment SDKs. Payments are not under test here.
for _mod in ("dodopayments", "razorpay", "sentry_sdk"):
    sys.modules.setdefault(_mod, mock.MagicMock())

import api.routes_enrichment as enr  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


@pytest.fixture()
def Session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (Candidate, Lead, OutreachOrder)])
    S = sessionmaker(bind=engine)
    s = S()
    s.add_all([
        Candidate(id=1, user_id="attacker", resume_text="."),
        Lead(id=1, candidate_id=1, name="n", company="c"),
        OutreachOrder(id=10, user_id="victim", candidate_id=2, status="leads_ready", leads_collected=500),
        OutreachOrder(id=20, user_id="attacker", candidate_id=1, status="leads_ready", leads_collected=800),
    ])
    s.commit()
    s.close()
    return S


def _run(Session, user_id, order_id, enrich_result=None):
    enr._enrichment_jobs["j"] = {"status": "running"}
    with mock.patch.object(enr, "SessionLocal", Session), \
         mock.patch("services.enrichment.enrichment_service._enrich_single_lead", return_value=enrich_result), \
         mock.patch.object(enr.time, "sleep"), \
         mock.patch.object(enr, "capture"):
        enr._run_enrichment_in_background("j", candidate_id=1, limit=1, user_id=user_id, order_id=order_id)


def test_foreign_order_is_untouched(Session):
    _run(Session, "attacker", 10)
    s = Session()
    victim = s.get(OutreachOrder, 10)
    assert victim.status == "leads_ready"
    assert victim.leads_collected == 500


def test_own_order_still_advances(Session):
    _run(Session, "attacker", 20)
    s = Session()
    own = s.get(OutreachOrder, 20)
    assert own.status == "enrichment_complete"
    assert own.leads_collected == 0
