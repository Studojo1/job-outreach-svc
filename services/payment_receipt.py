"""Receipt email after an outreach payment (audit PS-N10, 29 Sep 2026).

Studojo sent no receipt or next step after a purchase: the emailer had a
payment template nobody called. Every paid path (Razorpay and Dodo verify,
both webhooks, the stranded-order reconciler) calls send_receipt after its
commit. One receipt per payment order: the claim is a system_events row whose
primary key is the order id, so two paths confirming the same payment (or two
replicas) cannot both send.
"""
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.config import settings
from core.logger import get_logger
from database.models import PaymentOrder, SystemEvent, User

logger = get_logger(__name__)


def _amount(order: PaymentOrder) -> Optional[str]:
    if not order.amount_cents:
        return None
    major = order.amount_cents / 100
    if (order.currency or "").upper() == "INR":
        return f"Rs {major:,.0f}"
    return f"${major:,.2f}".replace(".00", "")


def _plan_name(order: PaymentOrder) -> str:
    try:
        from core.pricing import get_plan
        return get_plan(order.plan_id).label if order.plan_id else "Outreach"
    except Exception:  # noqa: BLE001 - an unknown plan id still gets a receipt
        return "Outreach"


def send_receipt(db: Session, order: PaymentOrder) -> bool:
    """Email the receipt once. Never raises: the payment is already committed."""
    if db is None:
        return False
    try:
        amount = _amount(order)
        if amount is None:
            return False  # free (100% coupon) orders get no receipt
        user = db.get(User, order.user_id)
        if user is None or not user.email:
            return False
        claim = SystemEvent(id=f"payment_receipt:{order.id}", event_type="payment_receipt_sent",
                            user_id=str(order.user_id), meta={"payment_order_id": order.id})
        db.add(claim)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return False  # already sent by another path
        from services.launch_nudge import _send_template
        ok = _send_template({
            "to": user.email,
            "template": "payment-thankyou",
            "user_name": (user.name or "").split(" ")[0] or "there",
            "plan_name": _plan_name(order),
            "credits": order.credits_granted or 0,
            "amount": amount,
            "order_id": str(order.razorpay_order_id or order.dodo_checkout_id or order.id),
            "action_url": f"{settings.FRONTEND_URL.rstrip('/')}/outreach/campaign/setup",
        })
        if not ok:
            db.delete(claim)  # let a later path try again
            db.commit()
        return ok
    except Exception:
        logger.exception("[RECEIPT] could not send the receipt for payment order %s", order.id)
        if db is not None:
            db.rollback()
        return False
