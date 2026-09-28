"""B2C open items EX-01 and UC-Q05.

- EX-01: /debug/logs and /debug/console served live server logs to anyone on
  api.studojo.com, and /api/v1/auth/debug-cookies echoed request cookies.
- UC-Q05: with RAZORPAY_WEBHOOK_SECRET empty (as in production), the Razorpay
  webhook skipped signature checks and accepted any payload.
"""
import asyncio
import hashlib
import hmac
import json
import pathlib
import sys

import pytest
from fastapi import HTTPException
from starlette.requests import Request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api import routes_payment
from api.routes_auth import router as auth_router
from core.config import settings


def _webhook_request(body: bytes, signature: str | None) -> Request:
    headers = [(b"content-type", b"application/json")]
    if signature is not None:
        headers.append((b"x-razorpay-signature", signature.encode()))
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/api/v1/payment/webhook", "headers": headers}
    return Request(scope, receive)


_CAPTURED = json.dumps({
    "event": "payment.captured",
    "payload": {"payment": {"entity": {"order_id": "order_x", "id": "pay_x"}}},
}).encode()


def _call(body: bytes, signature: str | None):
    return asyncio.run(routes_payment.razorpay_webhook(_webhook_request(body, signature), db=None))


def test_webhook_rejects_everything_when_secret_unset(monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", "")
    with pytest.raises(HTTPException) as exc:
        _call(_CAPTURED, None)
    assert exc.value.status_code == 503


def test_webhook_rejects_bad_signature(monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", "whsec")
    with pytest.raises(HTTPException) as exc:
        _call(_CAPTURED, "not-the-signature")
    assert exc.value.status_code == 400


def test_webhook_accepts_valid_signature(monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", "whsec")
    body = json.dumps({"event": "order.paid"}).encode()  # ignored event: no DB work
    sig = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    _call(body, sig)


def test_debug_routes_are_gone():
    from api.main import app

    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/debug/logs" not in paths
    assert "/debug/console" not in paths
    assert not any(p.endswith("/debug-cookies") for p in paths)
    assert not any(getattr(r, "path", "").endswith("/debug-cookies") for r in auth_router.routes)
