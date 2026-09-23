"""X-User-Id is only trusted next to the shared internal secret.

Before this, get_current_user returned whatever user the X-User-Id header named,
with no proof the header came from our own server-side caller. The frontend's
server-api.ts already sends `x-studojo-internal: <INTERNAL_API_SECRET>` beside it;
the backend just never checked it.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# Required config values are stubbed in conftest.py.
from starlette.requests import Request

from api import dependencies


def _request(headers: dict) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw})


def test_no_secret_configured_never_trusts_the_header(monkeypatch):
    monkeypatch.setattr(dependencies.settings, "INTERNAL_API_SECRET", "")
    req = _request({"X-User-Id": "victim", "x-studojo-internal": ""})
    assert dependencies._trusted_internal_caller(req) is False


def test_missing_or_wrong_secret_is_not_trusted(monkeypatch):
    monkeypatch.setattr(dependencies.settings, "INTERNAL_API_SECRET", "s3cret")
    assert dependencies._trusted_internal_caller(_request({"X-User-Id": "victim"})) is False
    assert dependencies._trusted_internal_caller(
        _request({"X-User-Id": "victim", "x-studojo-internal": "guess"})
    ) is False


def test_matching_secret_is_trusted(monkeypatch):
    monkeypatch.setattr(dependencies.settings, "INTERNAL_API_SECRET", "s3cret")
    req = _request({"X-User-Id": "u1", "x-studojo-internal": "s3cret"})
    assert dependencies._trusted_internal_caller(req) is True
