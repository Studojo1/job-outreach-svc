"""Resume upload parsing: OP-N06 (scanned-resume OCR), OP-N07 (is this a
resume at all), OP-N08 (NUL bytes). 29 Sep B2C audit, outreach-pre-payment.

Every test goes through the real parser (parse_resume / extract_text_from_pdf)
with only the network call to Azure replaced.
"""
import asyncio
from unittest.mock import MagicMock

import fitz
import pytest
import requests
from fastapi import BackgroundTasks, HTTPException

import api.routes_candidate as rc
from services.candidate_intelligence import parser


def _image_only_pdf() -> bytes:
    """A PDF with no text layer, like a scan or a Canva export."""
    doc = fitz.open()
    page = doc.new_page()
    page.draw_rect(fitz.Rect(50, 50, 200, 200), color=(0, 0, 0), fill=(0.5, 0.5, 0.5))
    data = doc.tobytes()
    doc.close()
    return data


def _text_pdf(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(40, 40, 560, 800), text, fontsize=9)
    data = doc.tobytes()
    doc.close()
    return data


class _Resp:
    def __init__(self, status, content=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._content = content
        self.text = "" if content is None else str(content)

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


RESUME = (
    "Priya Rao\npriya@example.com\n\nEDUCATION\nB.Tech Computer Science, "
    "Anna University, 2025\n\nEXPERIENCE\nSoftware Intern, Acme (2024)\n"
    "Built a billing dashboard used by 40 staff.\n\nSKILLS\nPython, SQL, React\n"
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)


def _post_sequence(monkeypatch, *outcomes):
    calls = []

    def fake_post(*a, **kw):
        calls.append(kw.get("timeout"))
        out = outcomes[len(calls) - 1]
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(parser.requests, "post", fake_post)
    return calls


# ── OP-N06 ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("first", [
    _Resp(503), _Resp(429), requests.Timeout("read timed out"),
])
def test_a_transient_ocr_failure_is_retried_once(monkeypatch, first):
    calls = _post_sequence(monkeypatch, first, _Resp(200, RESUME))
    raw, preview = parser.parse_resume(_image_only_pdf(), "scan.pdf")
    assert len(calls) == 2
    assert "Priya Rao" in raw


def test_ocr_that_still_fails_is_not_reported_as_an_empty_file(monkeypatch):
    calls = _post_sequence(monkeypatch, _Resp(500), _Resp(502))
    with pytest.raises(parser.ScannedResumeUnreadable) as e:
        parser.parse_resume(_image_only_pdf(), "scan.pdf")
    assert len(calls) == 2
    assert "could not read your scanned resume" in str(e.value)
    assert str(e.value) != parser.NO_TEXT_MESSAGE


def test_a_4xx_is_not_retried_but_still_gets_the_scanned_message(monkeypatch):
    calls = _post_sequence(monkeypatch, _Resp(400))
    with pytest.raises(parser.ScannedResumeUnreadable):
        parser.parse_resume(_image_only_pdf(), "scan.pdf")
    assert len(calls) == 1


def test_ocr_that_reads_a_blank_page_is_still_empty(monkeypatch):
    """OCR worked and found nothing: that really is an empty file."""
    _post_sequence(monkeypatch, _Resp(200, ""))
    with pytest.raises(ValueError) as e:
        parser.parse_resume(_image_only_pdf(), "scan.pdf")
    assert not isinstance(e.value, parser.ScannedResumeUnreadable)
    assert str(e.value) == parser.NO_TEXT_MESSAGE


class _File:
    filename = "scan.pdf"

    def __init__(self, data):
        self._data = data

    async def read(self, n=-1):
        return self._data


class _User:
    id = "u1"


def test_upload_answers_503_with_the_scanned_message(monkeypatch):
    _post_sequence(monkeypatch, _Resp(500), _Resp(500))
    with pytest.raises(HTTPException) as e:
        asyncio.run(rc.upload_resume(BackgroundTasks(), _File(_image_only_pdf()), _User(), MagicMock()))
    assert e.value.status_code == 503
    assert e.value.detail == parser.SCANNED_RESUME_UNREADABLE


# ── OP-N08 ──────────────────────────────────────────────────────────────────
def test_nul_bytes_are_stripped_from_text_and_preview(monkeypatch):
    monkeypatch.setattr(parser, "extract_text_from_docx",
                        lambda b: "Priya\x00 Rao\n" + RESUME.replace("Python", "Pyth\x00on"))
    raw, preview = parser.parse_resume(b"ignored", "cv.docx")
    assert "\x00" not in raw
    assert "Python" in preview["skills"]
    assert all("\x00" not in str(v) for v in preview.values())


def test_nul_bytes_from_a_real_pdf_text_layer_are_stripped(monkeypatch):
    monkeypatch.setattr(parser, "extract_text_from_pdf", lambda b: RESUME + "\x00\x00")
    raw, _ = parser.parse_resume(b"ignored", "cv.pdf")
    assert "\x00" not in raw


# ── OP-N07 ──────────────────────────────────────────────────────────────────
def test_a_resume_looks_like_a_resume():
    raw, preview = parser.parse_resume(_text_pdf(RESUME), "cv.pdf")
    assert preview["looks_like_resume"] is True


@pytest.mark.parametrize("text", [
    "Quarterly Maintenance Invoice\nInvoice No 4471\nBill to: Acme Facilities Pvt Ltd\n"
    "HVAC servicing, 3 units  Rs 12,000\nGST 18%  Rs 2,160\nTotal due Rs 14,160\n"
    "Summary of services rendered in Q3. Payment due within 30 days.",
    "Benchmark Result MODA against Gemini for garment attribute extraction\n"
    "Dataset: 1,200 product images. Metrics: precision, recall, F1 per attribute.\n"
    "MODA F1 0.81, Gemini F1 0.77. Colour and sleeve length were the hardest attributes.",
])
def test_an_invoice_or_report_does_not(text):
    raw, preview = parser.parse_resume(_text_pdf(text), "file.pdf")
    assert preview["looks_like_resume"] is False


def test_upload_returns_the_flag(monkeypatch):
    monkeypatch.setattr(rc, "find_reusable_candidate", lambda db, uid: None)
    monkeypatch.setattr(rc, "find_identical_candidate_with_leads", lambda *a: None)
    import services.stage_tracking as st
    monkeypatch.setattr(st, "safe_mark_stage", lambda *a, **k: None)
    monkeypatch.setattr(rc, "capture", lambda *a, **k: None)
    invoice = _text_pdf("Quarterly Maintenance Invoice\nTotal due Rs 14,160 for HVAC servicing "
                        "of three units at the Acme office, payable within 30 days.")
    out = asyncio.run(rc.upload_resume(BackgroundTasks(), _File(invoice), _User(), MagicMock()))
    assert out["looks_like_resume"] is False
    assert out["preview"]["looks_like_resume"] is False
