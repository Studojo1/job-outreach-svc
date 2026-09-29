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


def test_a_parser_crash_is_a_generic_500_not_a_400(monkeypatch):
    """A bug in the parser is ours, not the student's: 500, no raw error text."""
    def crash(*a):
        raise KeyError("pdfminer internal state")

    monkeypatch.setattr(rc, "parse_resume", crash)
    with pytest.raises(HTTPException) as e:
        _upload(MagicMock())
    assert e.value.status_code == 500
    assert "pdfminer" not in e.value.detail


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


# ── UC-Q29: the real parser's failures reach the student as friendly 422s ──

def _upload_bytes(name, data):
    class F:
        filename = name

        async def read(self, n=-1):
            return data
    return asyncio.run(rc.upload_resume(BackgroundTasks(), F(), _User(), MagicMock()))


def _tiny_docx(text):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml",
                   '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>")
    return buf.getvalue()


@pytest.mark.parametrize("name,data,library_words", [
    ("cv.docx", b"not a zip", "zip"),
    ("cv.doc", b"\xd0\xcf\x11\xe0 old binary word", "zip"),
    ("cv.pdf", b"%PDF-1.4 garbage", "pdf file: "),
])
def test_a_corrupt_file_gets_student_copy_not_the_library_error(name, data, library_words):
    with pytest.raises(HTTPException) as e:
        _upload_bytes(name, data)
    assert e.value.status_code == 422
    assert library_words not in e.value.detail.lower()
    assert "Could not parse" not in e.value.detail
    assert "upload" in e.value.detail.lower()


def test_too_little_text_gets_the_scanned_copy_message():
    with pytest.raises(HTTPException) as e:
        _upload_bytes("cv.docx", _tiny_docx("Jane"))
    assert e.value.status_code == 422
    assert "scanned copy" in e.value.detail
