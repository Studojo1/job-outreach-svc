"""Re-uploading after discovery (B2C UC-Q14, UC-Q28).

- UC-Q14: the same resume again made a new candidate and reran discovery.
- UC-Q28: a different resume moved the browser to the new candidate while the
  order stayed frozen on the old one, so the two disagreed.
"""
import asyncio
import pathlib
import sys
from datetime import datetime, timedelta

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import api.routes_candidate as rc
from database.models import Base, Candidate, Lead, OutreachOrder

RESUME = "Jane Doe. Product intern at Acme. Python, SQL, analytics. " * 3


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Candidate.__table__, Lead.__table__, OutreachOrder.__table__])
    s = sessionmaker(bind=engine)()
    old = datetime.utcnow() - timedelta(days=2)
    s.add(Candidate(id=1, user_id="u1", resume_text=RESUME, target_roles=["PM"], created_at=old))
    s.add(Lead(candidate_id=1, name="HM", company="Acme"))
    s.commit()
    monkeypatch.setattr(rc, "capture", lambda *a, **k: None)
    yield s
    s.close()


@pytest.fixture()
def upload(db, monkeypatch):
    class _F:
        filename = "cv.pdf"

        async def read(self, n=-1):
            return b"%PDF"

    class _U:
        id = "u1"

    def go(text):
        monkeypatch.setattr(rc, "parse_resume", lambda *a: (text, {"name": "Jane"}))
        return asyncio.run(rc.upload_resume(BackgroundTasks(), _F(), _U(), db))
    return go


def _order(db, **kw):
    o = OutreachOrder(user_id="u1", candidate_id=1, status="leads_ready",
                      leads_generated_at=datetime.utcnow() - timedelta(days=1),
                      created_at=datetime.utcnow() - timedelta(days=2), **kw)
    db.add(o)
    db.commit()
    return o


def test_identical_resume_returns_the_candidate_with_leads(db, upload):
    out = upload(RESUME)
    assert out["candidate_id"] == 1
    assert out["existing_results"] is True
    assert db.query(Candidate).count() == 1


def test_a_different_resume_still_gets_a_new_candidate(db, upload):
    out = upload(RESUME + " Updated.")
    assert out["candidate_id"] != 1
    assert "existing_results" not in out


def test_unpaid_order_gets_a_new_order_on_the_new_resume(db, upload):
    old = _order(db)
    out = upload(RESUME + " Updated.")
    new_id = out["candidate_id"]
    db.expire_all()
    orders = db.query(OutreachOrder).order_by(OutreachOrder.created_at.desc()).all()
    assert len(orders) == 2
    assert orders[0].candidate_id == new_id  # the active order follows the browser
    assert orders[0].resume_uploaded_at is not None
    assert db.get(OutreachOrder, old.id).candidate_id == 1  # old leads untouched
    assert "order_candidate_id" not in out


def test_paid_order_keeps_its_leads_and_the_client_is_told(db, upload):
    _order(db, payment_made_at=datetime.utcnow())
    out = upload(RESUME + " Updated.")
    assert out["order_candidate_id"] == 1
    assert db.query(OutreachOrder).count() == 1
    assert db.query(OutreachOrder).one().candidate_id == 1
