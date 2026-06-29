"""Payment reconciliation daemon.

Safety net for the case where the provider webhook never fires and the
client-side verify round-trip does not complete (tab closed, network drop,
redirect failure). Without this, a genuinely-paid order is stranded at
status='created' forever — invisible in the Campaign Health dashboard and
with no credits granted.

This daemon periodically re-checks recent `created` orders directly against
the payment provider (Razorpay / Dodo) and flips the ones that actually
captured, reusing the exact same finalisation path as the verify endpoints.

It is idempotent and read-mostly: it only writes when the provider confirms
a capture, and skips orders already paid/failed.
"""

import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta

from database.session import SessionLocal
from database.models import PaymentOrder, Coupon
from core.config import settings

logger = logging.getLogger(__name__)

# How far back to look for stranded orders, and how often to run.
RECONCILE_LOOKBACK_HOURS = 48
RECONCILE_INTERVAL_SECONDS = 600  # every 10 minutes
# Don't touch orders younger than this — give the normal verify flow time to win.
RECONCILE_MIN_AGE_MINUTES = 5

_stop = threading.Event()
_thread: threading.Thread | None = None


def _flip_to_paid(db, order: PaymentOrder, *, payment_id: str | None, provider: str) -> None:
    """Mark an order paid and run the shared finalisation path (credits +
    funnel stage). Mirrors the verify endpoints exactly."""
    from api.routes_payment import _finalize_credits
    from services.stage_tracking import safe_mark_stage

    # Re-read under a row lock and re-check status to avoid a double-grant race
    # between the two svc replicas (or against a concurrent verify call).
    locked = (
        db.query(PaymentOrder)
        .filter(PaymentOrder.id == order.id)
        .with_for_update(skip_locked=True)
        .first()
    )
    if locked is None or locked.status != "created":
        db.rollback()
        return
    order = locked

    if provider == "razorpay" and payment_id:
        order.razorpay_payment_id = payment_id
    elif provider == "dodo" and payment_id:
        order.dodo_payment_id = payment_id

    order.status = "paid"
    order.updated_at = datetime.utcnow()
    _finalize_credits(db, order)

    if order.coupon_id:
        db.query(Coupon).filter_by(id=order.coupon_id).update({"uses": Coupon.uses + 1})

    db.commit()
    logger.warning(
        "[RECONCILER] Recovered stranded order id=%s provider=%s plan=%s user=%s "
        "(webhook/verify never completed)",
        order.id, provider, order.plan_id, order.user_id,
    )
    safe_mark_stage(db, str(order.user_id), "payment_made")


def _check_razorpay(db, order: PaymentOrder) -> bool:
    """Return True if the order was flipped to paid."""
    if not order.razorpay_order_id:
        return False
    try:
        from api.routes_payment import _get_razorpay_client
        client = _get_razorpay_client()
        # Fetch all payment attempts for this order; a captured one == real money.
        payments = client.order.payments(order.razorpay_order_id)
        for p in payments.get("items", []):
            if p.get("status") == "captured":
                _flip_to_paid(db, order, payment_id=p.get("id"), provider="razorpay")
                return True
    except Exception as e:
        logger.error("[RECONCILER] Razorpay check failed for order id=%s: %s", order.id, e)
    return False


def _check_dodo(db, order: PaymentOrder) -> bool:
    if not order.dodo_checkout_id:
        return False
    try:
        from services import dodo_payments as dodo_svc
        status = asyncio.run(dodo_svc.get_checkout_status(order.dodo_checkout_id))
        if status.get("status") in ("succeeded", "paid", "complete", "completed"):
            _flip_to_paid(db, order, payment_id=status.get("payment_id"), provider="dodo")
            return True
    except Exception as e:
        logger.error("[RECONCILER] Dodo check failed for order id=%s: %s", order.id, e)
    return False


def _reconcile_once() -> None:
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        lookback = now - timedelta(hours=RECONCILE_LOOKBACK_HOURS)
        max_created = now - timedelta(minutes=RECONCILE_MIN_AGE_MINUTES)

        stranded = (
            db.query(PaymentOrder)
            .filter(
                PaymentOrder.status == "created",
                PaymentOrder.amount_cents > 0,
                PaymentOrder.provider != "coupon",
                PaymentOrder.created_at >= lookback,
                PaymentOrder.created_at <= max_created,
            )
            .order_by(PaymentOrder.created_at.desc())
            .all()
        )
        if not stranded:
            return

        recovered = 0
        for order in stranded:
            if order.provider == "razorpay":
                recovered += _check_razorpay(db, order)
            elif order.provider == "dodo":
                recovered += _check_dodo(db, order)

        if recovered:
            logger.warning("[RECONCILER] Recovered %d/%d stranded order(s) this cycle",
                           recovered, len(stranded))
        else:
            logger.info("[RECONCILER] Checked %d stranded order(s), none captured", len(stranded))
    finally:
        db.close()


def _loop() -> None:
    logger.info("[RECONCILER] Payment reconciliation loop started (interval=%ds, lookback=%dh)",
                RECONCILE_INTERVAL_SECONDS, RECONCILE_LOOKBACK_HOURS)
    while not _stop.is_set():
        try:
            _reconcile_once()
        except Exception as e:
            logger.error("[RECONCILER] Error in reconcile loop: %s", e, exc_info=True)
        # Sleep in 1s chunks so shutdown is responsive.
        for _ in range(RECONCILE_INTERVAL_SECONDS):
            if _stop.is_set():
                break
            time.sleep(1)
    logger.info("[RECONCILER] Payment reconciliation loop stopped")


def start_reconciler() -> None:
    """Start the background reconciliation loop. Called once at app startup."""
    global _thread
    if _thread and _thread.is_alive():
        logger.warning("[RECONCILER] Reconciler already running")
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="payment-reconciler")
    _thread.start()
    logger.info("[RECONCILER] Background reconciler started")


def stop_reconciler() -> None:
    _stop.set()
