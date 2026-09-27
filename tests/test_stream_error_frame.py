"""An unexpected failure in a quiz turn must reach the student as an error
frame, not a bare 500 (quiz audit Q16, still open on main on 27 Sep).

The earlier fix replaced the KeyError sources the audit named and stopped
there. The prescribed catch-all was never added, so the next unpredicted
failure would have produced the same bare 500.
"""
import asyncio
import json
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import api.routes_candidate as rc


class _Candidate:
    id = 7
    user_id = "u1"
    resume_text = "resume"
    resume_profile = {"domain": "engineering", "likely_roles": ["Backend Engineer"]}
    parsed_json = {"_qps": {"domain": "engineering", "likely_roles": ["Backend Engineer"]}}
    quiz_answers = None


class _User:
    id = "u1"


def _db(candidate):
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = candidate
    return db


async def _frames(response):
    out = []
    async for chunk in response.body_iterator:
        out.append(chunk if isinstance(chunk, str) else chunk.decode())
    return "".join(out)


def _run(coro):
    return asyncio.run(coro)


def test_an_unexpected_error_becomes_an_error_frame(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(rc, "_chat_stream_turn", boom)
    req = rc.ChatRequest(message="x", chat_history=[{"role": "user", "content": "x"}])
    resp = _run(rc.candidate_chat_stream(7, req, _User(), _db(_Candidate())))

    assert resp.status_code == 200
    body = _run(_frames(resp))
    frame = json.loads(body.removeprefix("data: ").strip())
    assert frame["type"] == "error"
    # The student never sees the exception text.
    assert "nobody predicted" not in frame["message"]


def test_a_missing_candidate_is_still_a_404(monkeypatch):
    req = rc.ChatRequest(message="x", chat_history=[])
    with pytest.raises(HTTPException) as e:
        _run(rc.candidate_chat_stream(7, req, _User(), _db(None)))
    assert e.value.status_code == 404


def test_a_normal_turn_still_serves_the_next_question():
    req = rc.ChatRequest(
        message="Student, not graduating soon",
        chat_history=[
            {"role": "assistant", "content": "Q1"},
            {"role": "user", "content": "Student, not graduating soon"},
        ],
    )
    resp = _run(rc.candidate_chat_stream(7, req, _User(), _db(_Candidate())))
    frame = json.loads(_run(_frames(resp)).removeprefix("data: ").strip())
    assert frame["type"] == "complete"
    assert frame["questions_asked_so_far"] == 2
