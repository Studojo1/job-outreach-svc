"""B2C UC-Q37: running discovery again must reuse a full lead set, not add
800 more leads from looser filters, unless the quiz changed afterwards."""
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

from api import routes_discovery as rd
from database.models import Base, Candidate, Lead, LeadScore


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


class _U:
    id = "u"


class _Ran(Exception):
    pass


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Candidate.__table__, Lead.__table__, LeadScore.__table__])
    s = sessionmaker(bind=engine)()
    old = datetime.utcnow() - timedelta(days=3)
    s.add(Candidate(id=1, user_id="u", resume_text=".", quiz_answers_updated_at=old - timedelta(hours=1)))
    s.add_all([Lead(candidate_id=1, name=f"HM{i}", company="Acme", created_at=old) for i in range(800)])
    s.commit()
    monkeypatch.setattr(rd, "capture", lambda *a, **k: None)
    import services.stage_tracking as st
    monkeypatch.setattr(st, "safe_advance_discovery_status", lambda *a, **k: None)
    # Anything past the guard means a new search would run.
    monkeypatch.setattr(rd, "collect_leads", lambda *a, **k: (_ for _ in ()).throw(_Ran()))
    yield s
    s.close()


def _search(db):
    return asyncio.run(rd.search_leads(rd.DiscoveryRequest(candidate_id=1), BackgroundTasks(), current_user=_U(), db=db))


def test_a_full_set_from_days_ago_is_reused(db):
    out = _search(db)
    assert out["idempotent"] is True and out["leads_collected"] == 800
    assert db.query(Lead).count() == 800


def test_a_quiz_re_answered_after_the_leads_searches_again(db, caplog):
    c = db.get(Candidate, 1)
    c.quiz_answers_updated_at = datetime.utcnow()
    db.commit()
    caplog.set_level("INFO")
    out = None
    try:
        out = _search(db)
    except Exception:  # noqa: S110 - the new search is stubbed; getting past the guard is the point
        pass
    assert not (out or {}).get("idempotent")
    assert any("re-answered the quiz" in r.getMessage() for r in caplog.records)
