"""Alarm when a payment provider's webhooks all fail their signature check.

On 30 Sep every genuine Razorpay delivery was rejected 400 "Signature
mismatch" for hours (audit UC-Q05 / CF-N02: the secret on the deployment did
not match the dashboard's). Nothing noticed, because a rejected webhook is not
lost money: the reconciler still confirms the payment within minutes. What is
lost is the fast path, and the audit found it only by reading logs.

Each pod keeps the recent signature results per provider. When a provider has
had FAIL_THRESHOLD or more failures and not one success inside WINDOW, the pod
logs at CRITICAL and emails the founders (ops-alert). The email goes out at
most once per ALERT_EVERY across all pods (the marker is a system_events row).

Deliberately in-memory and cheap: the webhook URLs are public, so nothing
here writes to the database per request. An attacker posting junk can at
worst trigger the same alert a real outage would, at most once per ALERT_EVERY.
"""

import logging
import threading
from collections import deque
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

WINDOW = timedelta(hours=2)
FAIL_THRESHOLD = 3
ALERT_EVERY = timedelta(hours=6)
ALERT_EVENT = "payment_webhook_signature_alert"

_lock = threading.Lock()
_results: dict[str, deque] = {}


def record(provider: str, ok: bool, now: datetime | None = None) -> bool:
    """Record one signature check. Returns True when this call raised the alarm."""
    now = now or datetime.utcnow()
    with _lock:
        q = _results.setdefault(provider, deque(maxlen=200))
        q.append((now, ok))
        while q and now - q[0][0] > WINDOW:
            q.popleft()
        if ok:
            return False
        failures = sum(1 for _, r in q if not r)
        if failures < FAIL_THRESHOLD or any(r for _, r in q):
            return False
    logger.critical(
        "[WEBHOOK_ALERT] %s: %d webhook(s) in the last %s all failed the signature check. "
        "The webhook secret on the deployment probably does not match the provider dashboard; "
        "payments are only being confirmed by the reconciler.",
        provider, failures, WINDOW,
    )
    threading.Thread(target=_alert_founders, args=(provider, failures, now), daemon=True,
                     name="webhook-signature-alert").start()
    return True


def _alert_founders(provider: str, failures: int, now: datetime) -> None:
    try:
        from sqlalchemy import func

        from database.models import SystemEvent
        from database.session import SessionLocal
        db = SessionLocal()
        try:
            last = (db.query(func.max(SystemEvent.created_at))
                    .filter(SystemEvent.event_type == f"{ALERT_EVENT}:{provider}")
                    .scalar())
            if last is not None and now - last < ALERT_EVERY:
                return
            db.add(SystemEvent(event_type=f"{ALERT_EVENT}:{provider}", created_at=now,
                               meta={"provider": provider, "failures": failures}))
            db.commit()
        finally:
            db.close()
        from services.reconcile import _tell_founders
        _tell_founders(
            f"{provider} webhooks are failing their signature check",
            f"{failures} {provider} webhook deliveries in the last {WINDOW} were rejected for a bad "
            "signature and none succeeded. The webhook secret on the job-outreach-svc deployment "
            f"probably no longer matches the one in the {provider} dashboard. Payments are still "
            "confirmed by the reconciler, but late, and refunds made in the dashboard are not "
            "written back until this is fixed.",
        )
    except Exception:
        logger.exception("[WEBHOOK_ALERT] could not send the founder alert for %s", provider)


def reset() -> None:
    """Tests only."""
    with _lock:
        _results.clear()
