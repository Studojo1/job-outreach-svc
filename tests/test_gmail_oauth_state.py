"""Gmail OAuth: the state is signed, and only the user who started the flow
can finish it (audit N03).

state used to be the bare user id, so a Gmail could be attached to any
account. The reverse was worse: start a flow on your own account, get a
victim to approve it, and their inbox (read scope) lands on your account.
"""
import asyncio
import pathlib
import sys
import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from fastapi import HTTPException

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api import routes_gmail
from services.authentication import google_oauth


def test_state_round_trips():
    assert google_oauth.verify_gmail_state(google_oauth.sign_gmail_state("u1")) == "u1"


@pytest.mark.parametrize("state", [
    "u1",                                                               # the old bare user id
    jwt.encode({"sub": "u1", "purpose": "gmail_oauth_state", "exp": int(time.time()) + 60},
               "guessed-key", algorithm="HS256"),                      # wrong key
    jwt.encode({"sub": "u1", "purpose": "something_else", "exp": int(time.time()) + 60},
               google_oauth._state_key(), algorithm="HS256"),          # other purpose
    jwt.encode({"sub": "u1", "purpose": "gmail_oauth_state", "exp": int(time.time()) - 1},
               google_oauth._state_key(), algorithm="HS256"),          # expired
])
def test_bad_states_are_rejected(state):
    assert google_oauth.verify_gmail_state(state) is None


def test_auth_url_carries_a_signed_state():
    url = google_oauth.generate_gmail_auth_url("u1")
    state = parse_qs(urlparse(url).query)["state"][0]
    assert state != "u1" and google_oauth.verify_gmail_state(state) == "u1"


def test_callback_hands_the_code_to_the_page_and_stores_nothing():
    state = google_oauth.sign_gmail_state("u1")
    resp = asyncio.run(routes_gmail.gmail_oauth_callback(code="c0de", state=state))
    q = parse_qs(urlparse(resp.headers["location"]).query)
    assert q["gmail_code"] == ["c0de"] and q["gmail_state"] == [state]


def test_callback_rejects_a_forged_state():
    resp = asyncio.run(routes_gmail.gmail_oauth_callback(code="c0de", state="victim-user-id"))
    assert "invalid_state" in resp.headers["location"]


class _U:
    def __init__(self, uid):
        self.id = uid


def test_completing_someone_elses_flow_is_refused(monkeypatch):
    called = []
    monkeypatch.setattr(routes_gmail, "exchange_gmail_code", lambda code: called.append(code))
    state = google_oauth.sign_gmail_state("attacker")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes_gmail.gmail_oauth_complete(
            routes_gmail.GmailCompleteRequest(code="c", state=state), current_user=_U("victim"), db=None))
    assert exc.value.status_code == 403
    assert called == []  # the code is never even exchanged


def test_expired_state_is_refused_at_complete():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes_gmail.gmail_oauth_complete(
            routes_gmail.GmailCompleteRequest(code="c", state="u1"), current_user=_U("u1"), db=None))
    assert exc.value.status_code == 400


def test_own_flow_connects(monkeypatch):
    stored = {}

    async def exchange(code):
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 3599,
                "scope": "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.readonly"}

    async def info(token):
        return {"email": "me@gmail.com"}

    async def store(**kw):
        stored.update(kw)

    class _Q:
        def filter_by(self, **kw): return self
        def first(self): return None

    class _DB:
        def query(self, *a): return _Q()

    monkeypatch.setattr(routes_gmail, "exchange_gmail_code", exchange)
    monkeypatch.setattr(routes_gmail, "get_google_user_info", info)
    monkeypatch.setattr(routes_gmail, "store_user_tokens", store)
    monkeypatch.setattr(routes_gmail, "capture", lambda *a, **k: None)
    monkeypatch.setattr(routes_gmail, "_resume_auth_paused_campaigns", lambda *a: None)
    out = asyncio.run(routes_gmail.gmail_oauth_complete(
        routes_gmail.GmailCompleteRequest(code="c", state=google_oauth.sign_gmail_state("u1")),
        current_user=_U("u1"), db=_DB()))
    assert out["status"] == "connected" and stored["user_id"] == "u1"


def test_cancel_on_googles_screen_returns_to_the_app():
    """NEW-02: ?error=access_denied has no code; it used to be a raw 422."""
    resp = asyncio.run(routes_gmail.gmail_oauth_callback(error="access_denied", state="x"))
    assert resp.status_code == 307
    assert "status=error&message=cancelled" in resp.headers["location"]


def test_someone_elses_mailbox_is_not_taken_over():
    """Connecting a Gmail another account already holds must not move its row."""
    from services.authentication import token_manager

    class _Acct:
        user_id = "owner"
        email_address = "shared@gmail.com"

    class _Q:
        def __init__(self, hit): self.hit = hit
        def filter(self, *a): return self
        def first(self): return self.hit

    class _DB:
        calls = 0
        def query(self, *a):
            _DB.calls += 1
            return _Q(None if _DB.calls == 1 else _Acct())   # none by user, one by address
        def rollback(self): pass
        def commit(self): raise AssertionError("must not commit")

    with pytest.raises(token_manager.MailboxOwnedElsewhere):   # raised before any write
        asyncio.run(token_manager.store_user_tokens(
            _DB(), "intruder", "shared@gmail.com", "at", "rt", 3599))


def test_complete_turns_a_taken_mailbox_into_409(monkeypatch):
    from services.authentication.token_manager import MailboxOwnedElsewhere

    async def exchange(code):
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 3599,
                "scope": "gmail.send gmail.readonly"}

    async def info(token):
        return {"email": "shared@gmail.com"}

    async def store(**kw):
        raise MailboxOwnedElsewhere(kw["email_address"])

    monkeypatch.setattr(routes_gmail, "exchange_gmail_code", exchange)
    monkeypatch.setattr(routes_gmail, "get_google_user_info", info)
    monkeypatch.setattr(routes_gmail, "store_user_tokens", store)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes_gmail.gmail_oauth_complete(
            routes_gmail.GmailCompleteRequest(code="c", state=google_oauth.sign_gmail_state("u1")),
            current_user=_U("u1"), db=None))
    assert exc.value.status_code == 409
