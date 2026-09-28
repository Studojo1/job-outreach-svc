"""Refund a payment through its provider and settle it in the app (audit P05).

There was no refund path at all: no provider refund call anywhere, and
payment_orders had no refunded state, so every refund was a manual click in
the Razorpay or Dodo dashboard with nothing written back. 19 paying users had
received nothing when the audit ran.

refund_payment, in order:
  1. refuses anything that is not a paid, real-money order, or already refunded;
  2. asks the provider to refund the full amount (nothing in the app changes
     unless the provider accepts);
  3. cancels the user's unfinished campaigns so their reserved credits come back;
  4. marks the order refunded (amount, time, provider refund id) and revokes the
     credits that payment bought, ledgered as revoke_refund. Credits already
     used on delivered emails cannot be revoked and are reported instead.
"""

import asyncio
from datetime import datetime

from sqlalchemy.orm import Session

from core.logger import get_logger
from database.models import Campaign, Candidate, PaymentOrder
from services import credits

logger = get_logger(__name__)


class RefundError(Exception):
    pass


async def _provider_refund(order: PaymentOrder, reason: str) -> str:
    """Full refund at the provider. Returns the provider's refund id."""
    if order.provider == "dodo":
        if not order.dodo_payment_id:
            raise RefundError("This Dodo order has no payment id to refund.")
        from services.dodo_payments import _get_client
        refund = await _get_client().refunds.create(payment_id=order.dodo_payment_id, reason=reason[:200])
        return str(getattr(refund, "refund_id", None) or getattr(refund, "id", ""))
    if order.provider == "razorpay":
        if not order.razorpay_payment_id:
            raise RefundError("This Razorpay order has no payment id to refund.")
        from api.routes_payment import _get_razorpay_client
        client = _get_razorpay_client()
        refund = await asyncio.to_thread(
            client.payment.refund, order.razorpay_payment_id,
            {"amount": order.amount_cents, "notes": {"reason": reason[:200], "order": str(order.id)}},
        )
        return str(refund.get("id", ""))
    raise RefundError(f"Cannot refund a '{order.provider}' order through a provider.")


async def refund_payment(db: Session, order_id: int, *, actor: str, reason: str) -> dict:
    order = db.get(PaymentOrder, order_id)
    if order is None:
        raise RefundError("Payment not found.")
    if order.status == "refunded":
        raise RefundError("This payment is already refunded.")
    if order.status not in ("paid", "completed") or not order.amount_cents or order.amount_cents <= 0:
        raise RefundError("Only a paid, real-money payment can be refunded.")

    # 2. The provider first: if it refuses, nothing in the app changes.
    refund_id = await _provider_refund(order, reason)

    # 3. Money is back, so stop the service: unfinished campaigns are cancelled
    #    (their unsent work retired, their credits released through the ledger).
    from services.email_campaign.campaign_worker import finish_campaign
    live = (
        db.query(Campaign).join(Candidate, Candidate.id == Campaign.candidate_id)
        .filter(Candidate.user_id == order.user_id,
                Campaign.status.in_(("draft", "running", "paused")))
        .all()
    )
    for c in live:
        finish_campaign(db, c, reason=f"payment {order.id} refunded", final_status="cancelled")

    # 4. Settle.
    now = datetime.utcnow()
    order.status = "refunded"
    order.refunded_cents = order.amount_cents
    order.refunded_at = now
    order.refund_id = refund_id
    order.updated_at = now
    bought = order.credits_granted or 0
    revoked = credits.revoke(db, order.user_id, bought, credits.REVOKE_REFUND,
                             payment_order_id=order.id, actor=actor, note=reason[:200])
    db.commit()
    logger.info("[REFUND] order %s refunded by %s: %s cents, %s of %s credits revoked, refund_id=%s",
                order.id, actor, order.amount_cents, revoked, bought, refund_id)
    return {
        "order_id": order.id,
        "status": "refunded",
        "refunded_cents": order.amount_cents,
        "currency": order.currency,
        "refund_id": refund_id,
        "campaigns_cancelled": [c.id for c in live],
        "credits_revoked": revoked,
        "credits_already_used": bought - revoked,
    }
