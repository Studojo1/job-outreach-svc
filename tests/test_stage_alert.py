"""B2C UC-Q16: a failed order write at upload is swallowed, but not silently.

On 23 Sep every upload's order write failed for 8 hours after a schema drop
and 9 students were lost; safe_mark_stage swallowed it and nobody was told.
"""
import asyncio
import logging
from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import api.routes_candidate as rc
import database.session as dbs
import services.stage_tracking as st
from database.models import Base, SystemEvent


@pytest.fixture()
def events(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[SystemEvent.__table__])
    monkeypatch.setattr(dbs, "SessionLocal", sessionmaker(bind=engine))

    def broken(*a, **k):
        raise RuntimeError('column "psychometric_profile" does not exist')
    monkeypatch.setattr(st, "mark_stage", broken)
    return lambda: dbs.SessionLocal().query(SystemEvent).all()


def test_failed_stage_write_is_recorded_for_ops(events, caplog):
    db = MagicMock()
    with caplog.at_level(logging.ERROR):
        st.safe_mark_stage(db, "u1", "resume_uploaded", candidate_id=5)
    [ev] = events()
    assert ev.event_type == "stage_tracking_failed" and ev.user_id == "u1"
    assert ev.meta["stage"] == "resume_uploaded"
    assert "psychometric_profile" in ev.meta["error"]
    assert "[STAGE_ALERT]" in caplog.text
    db.rollback.assert_called()  # the request's session is usable afterwards


def test_upload_still_succeeds_when_the_order_write_fails(events, monkeypatch):
    monkeypatch.setattr(rc, "parse_resume", lambda *a: ("resume text " * 10, {"name": "A"}))
    monkeypatch.setattr(rc, "find_reusable_candidate", lambda db, uid: None)
    monkeypatch.setattr(rc, "capture", lambda *a, **k: None)

    class _F:
        filename = "cv.pdf"

        async def read(self, n=-1):
            return b"%PDF"

    class _U:
        id = "u1"
    out = asyncio.run(rc.upload_resume(BackgroundTasks(), _F(), _U(), MagicMock()))
    assert out["status"] == "success"
    assert [e.meta["stage"] for e in events()] == ["resume_uploaded"]
