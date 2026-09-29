"""Response size, campaign email list and Gmail health (UC-Q21, NEW-06, PP-P48)."""
import asyncio
import gzip
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from database.models import Base, Campaign, Candidate, EmailAccount, EmailSent, Lead, User


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


# ── UC-Q21 / NEW-06: gzip, but event streams still stream ────────────────────

async def _drive(app, path, accept="gzip"):
    """Run one GET through the ASGI app and return the messages it sent."""
    sent = []
    scope = {"type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
             "query_string": b"", "headers": [(b"accept-encoding", accept.encode())],
             "http_version": "1.1", "scheme": "http", "server": ("t", 80), "client": ("c", 1),
             "root_path": ""}

    requested = []

    async def receive():
        if not requested:
            requested.append(True)
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()  # client stays connected

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _app_with(middleware):
    app = FastAPI()
    app.add_middleware(middleware, minimum_size=1000)

    @app.get("/json")
    def big_json():
        return JSONResponse({"leads": [{"name": "x" * 40, "i": i} for i in range(200)]})

    @app.get("/sse")
    def sse():
        async def gen():
            for i in range(3):
                yield f"data: {{\"type\": \"chunk\", \"i\": {i}}}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def _headers(start):
    return {k.decode().lower(): v.decode() for k, v in start["headers"]}


def test_main_app_uses_sse_safe_gzip():
    from api.main import app
    from core.middleware import SSESafeGZipMiddleware
    assert any(m.cls is SSESafeGZipMiddleware for m in app.user_middleware)


def test_json_is_gzipped():
    from core.middleware import SSESafeGZipMiddleware
    msgs = asyncio.run(_drive(_app_with(SSESafeGZipMiddleware), "/json"))
    assert _headers(msgs[0]).get("content-encoding") == "gzip"
    assert b'"leads"' in gzip.decompress(msgs[1]["body"])


def test_event_stream_frames_go_out_uncompressed_one_by_one():
    from core.middleware import SSESafeGZipMiddleware
    msgs = asyncio.run(_drive(_app_with(SSESafeGZipMiddleware), "/sse"))
    assert "content-encoding" not in _headers(msgs[0])
    frames = [m["body"] for m in msgs[1:] if m.get("body")]
    # Each SSE frame leaves as its own readable chunk, not held in a gzip buffer.
    assert frames[:3] == [f'data: {{"type": "chunk", "i": {i}}}\n\n'.encode() for i in range(3)]


def test_small_json_is_not_gzipped():
    from core.middleware import SSESafeGZipMiddleware
    app = FastAPI()
    app.add_middleware(SSESafeGZipMiddleware, minimum_size=1000)
    app.get("/s")(lambda: {"ok": True})
    msgs = asyncio.run(_drive(app, "/s"))
    assert "content-encoding" not in _headers(msgs[0])


# ── NEW-06: campaign emails list ─────────────────────────────────────────────

@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[User.__table__, EmailAccount.__table__, Candidate.__table__,
                                             Lead.__table__, Campaign.__table__, EmailSent.__table__])
    session = sessionmaker(bind=engine)()
    now = datetime(2026, 9, 1)
    for uid in ("u1", "u2"):
        session.add(User(id=uid, email=f"{uid}@x.com", name=uid, created_at=now, updated_at=now))
    session.add(Candidate(id=1, user_id="u1"))
    session.add(Campaign(id=1, candidate_id=1, name="c", status="running"))
    session.commit()
    session.info["engine"] = engine
    return session


def _add_emails(db, n):
    for i in range(n):
        db.add(Lead(id=100 + i, candidate_id=1, name=f"L{i}", company="Co", title="T"))
        db.add(EmailSent(campaign_id=1, lead_id=100 + i, to_email=f"l{i}@co.com", subject="s",
                         body=f"body {i}", reply_text="thanks" if i == 0 else None, status="sent",
                         scheduled_at=datetime(2026, 9, 1, 10, i)))
    db.add(EmailSent(campaign_id=1, lead_id=None, subject="orphan", body="b", status="queued",
                     scheduled_at=datetime(2026, 9, 2)))
    db.commit()


def _count_queries(db):
    counter = {"n": 0}

    def _before(*a, **k):
        counter["n"] += 1

    event.listen(db.info["engine"], "before_cursor_execute", _before)
    return counter


def _user(uid="u1"):
    return SimpleNamespace(id=uid)


def test_emails_list_query_count_does_not_grow_with_emails(db):
    from api.routes_campaign import get_campaign_emails
    _add_emails(db, 30)
    counter = _count_queries(db)
    out = asyncio.run(get_campaign_emails(1, current_user=_user(), db=db))
    emails = out["emails"]
    assert len(emails) == 31
    assert counter["n"] <= 4  # campaign + candidate + one joined list query
    assert emails[0]["lead_name"] == "L0" and emails[0]["lead_company"] == "Co"
    # Current dashboard reads body/reply_text from the list; default keeps them.
    assert emails[0]["body"] == "body 0" and emails[0]["reply_text"] == "thanks"
    assert emails[-1]["lead_name"] == "Unknown"


def test_emails_list_summary_drops_bodies_and_detail_has_them(db):
    from api.routes_campaign import get_campaign_email, get_campaign_emails
    _add_emails(db, 3)
    out = asyncio.run(get_campaign_emails(1, fields="summary", current_user=_user(), db=db))
    assert all("body" not in e and "reply_text" not in e for e in out["emails"])
    first = out["emails"][0]
    detail = asyncio.run(get_campaign_email(1, first["id"], current_user=_user(), db=db))
    assert detail["body"] == "body 0" and detail["reply_text"] == "thanks" and detail["lead_name"] == "L0"


def test_email_detail_is_owner_only(db):
    from api.routes_campaign import get_campaign_email
    _add_emails(db, 1)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_campaign_email(1, 1, current_user=_user("u2"), db=db))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_campaign_email(1, 9999, current_user=_user(), db=db))
    assert exc.value.status_code == 404


# ── PP-P48: Gmail health check ───────────────────────────────────────────────

@pytest.fixture()
def gmail_db(db):
    for acc_id, uid in ((1, "u1"), (2, "u1"), (3, "u2")):
        db.add(EmailAccount(id=acc_id, user_id=uid, email_address=f"m{acc_id}@gmail.com", provider="gmail",
                            **{"access_token": "t", "refresh_token": f"r{acc_id}"}))
    db.commit()
    return db


def _fake_post(monkeypatch, outcomes, seen):
    from api import routes_gmail

    def post(url, data=None, timeout=None):
        seen.append((data["refresh_token"], threading.get_ident()))
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(status_code=outcome)

    monkeypatch.setattr(routes_gmail.http_requests, "post", post)


def _call(db, account_id=None, uid="u1"):
    from api.routes_gmail import get_gmail_account

    async def run():
        return await get_gmail_account(email_account_id=account_id, current_user=_user(uid), db=db), \
            threading.get_ident()

    return asyncio.run(run())


def test_gmail_check_retries_once_and_runs_off_loop(gmail_db, monkeypatch):
    seen = []
    _fake_post(monkeypatch, [TimeoutError("slow"), 200], seen)
    out, loop_thread = _call(gmail_db)
    assert out["token_valid"] is True
    assert len(seen) == 2
    assert all(tid != loop_thread for _, tid in seen)


def test_gmail_check_failure_is_unknown_not_invalid(gmail_db, monkeypatch):
    seen = []
    _fake_post(monkeypatch, [TimeoutError("slow"), ConnectionError("dns")], seen)
    out, _ = _call(gmail_db)
    assert out["token_valid"] is None


def test_gmail_check_revoked_is_false_without_retry(gmail_db, monkeypatch):
    seen = []
    _fake_post(monkeypatch, [400], seen)
    out, _ = _call(gmail_db)
    assert out["token_valid"] is False and len(seen) == 1


def test_gmail_check_honours_email_account_id(gmail_db, monkeypatch):
    seen = []
    _fake_post(monkeypatch, [200], seen)
    out, _ = _call(gmail_db, account_id=2)
    assert out["email_account_id"] == 2 and seen[0][0] == "r2"
    # Someone else's account id is not silently swapped for one of ours.
    with pytest.raises(HTTPException) as exc:
        _call(gmail_db, account_id=3)
    assert exc.value.status_code == 404


# ── UC-Q40: leads_viewed only counts orders from when it was tracked ─────────

def test_funnel_leads_viewed_counts_only_tracked_orders(db):
    from api.routes_admin import _funnel_aggregate
    from database.models import OutreachOrder
    Base.metadata.create_all(db.info["engine"], tables=[OutreachOrder.__table__])
    now = datetime(2026, 9, 1)
    for uid in ("u3", "u4", "u5"):
        db.add(User(id=uid, email=f"{uid}@x.com", name=uid, created_at=now, updated_at=now))
    gen = datetime(2026, 9, 20)
    # Before tracking: three users generated leads, none could have leads_viewed_at.
    for uid in ("u1", "u2", "u3"):
        db.add(OutreachOrder(user_id=uid, created_at=datetime(2026, 9, 20), resume_uploaded_at=gen,
                             quiz_completed_at=gen, leads_generated_at=gen))
    # After: two generated leads, one viewed them and one then reached payment.
    after = datetime(2026, 9, 27, 6, 0)
    db.add(OutreachOrder(user_id="u4", created_at=after, resume_uploaded_at=after, quiz_completed_at=after,
                         leads_generated_at=after, leads_viewed_at=after, payment_page_reached_at=after))
    db.add(OutreachOrder(user_id="u5", created_at=after, resume_uploaded_at=after, quiz_completed_at=after,
                         leads_generated_at=after))
    db.commit()
    funnel = {s["stage"]: s for s in _funnel_aggregate(db)}
    lv = funnel["leads_viewed"]
    assert lv["users_reached"] == 1
    assert (lv["drop_off_from_prev"], lv["drop_off_pct_from_prev"]) == (1, 50.0)  # vs 2 post-cutoff, not 5
    assert "since 27 Sep" in lv["label"] and lv["counted_since"].startswith("2026-09-27T05:00")
    # Next stage compares with the all-time leads_generated count (5), not the gated one.
    pay = funnel["payment_page_reached"]
    assert funnel["leads_generated"]["users_reached"] == 5
    assert (pay["users_reached"], pay["drop_off_from_prev"]) == (1, 4)
