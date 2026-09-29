"""Upload errors: the student's problem is a 400 they can act on, ours is a
generic 500 (quiz audit Q39, still open on main on 27 Sep).

The terminal handler used to be `except Exception as e: raise
HTTPException(400, detail=str(e))` around the whole handler, so a database
outage reached the student as "400 Bad Request: (psycopg2.OperationalError)...".
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks, HTTPException

import api.routes_candidate as rc


class _File:
    filename = "cv.pdf"

    async def read(self, n=-1):
        return b"%PDF-1.4"


class _User:
    id = "u1"


def _upload(db):
    return asyncio.run(rc.upload_resume(BackgroundTasks(), _File(), _User(), db))


def test_a_parse_error_is_a_400_with_the_parsers_message(monkeypatch):
    def bad(*a):
        raise ValueError("Unsupported file type: .png. Please upload a PDF or DOCX file.")

    monkeypatch.setattr(rc, "parse_resume", bad)
    with pytest.raises(HTTPException) as e:
        _upload(MagicMock())
    assert e.value.status_code == 400
    assert "Please upload a PDF or DOCX" in e.value.detail


def test_a_database_failure_is_a_generic_500(monkeypatch):
    monkeypatch.setattr(rc, "parse_resume", lambda *a: ("resume text", {"name": "A"}))
    monkeypatch.setattr(rc, "find_reusable_candidate", lambda db, uid: None)
    db = MagicMock()
    db.commit.side_effect = RuntimeError("(psycopg2.OperationalError) server closed the connection")

    with pytest.raises(HTTPException) as e:
        _upload(db)
    assert e.value.status_code == 500
    assert "psycopg2" not in e.value.detail
    db.rollback.assert_called()


def test_a_file_over_10mb_is_a_413_and_never_parsed(monkeypatch):
    """UC-Q31: the page promised 10MB while the API took up to 100MB."""
    parsed = []
    monkeypatch.setattr(rc, "parse_resume", lambda *a: parsed.append(a) or ("", {}))
    reads = []

    class _Big:
        filename = "huge.pdf"

        async def read(self, n=-1):
            reads.append(n)
            return b"x" * (rc.MAX_RESUME_BYTES + 1 if n == -1 else min(n, rc.MAX_RESUME_BYTES + 5))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(rc.upload_resume(BackgroundTasks(), _Big(), _User(), MagicMock()))
    assert exc.value.status_code == 413
    assert parsed == []
    assert reads == [rc.MAX_RESUME_BYTES + 1]  # bounded read, not the whole body
