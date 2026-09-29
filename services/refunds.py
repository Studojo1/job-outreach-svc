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


async def _provider_refund(order: PaymentOrder, reason: str, amount_cents: int | None = None) -> str:
    """Refund at the provider. amount_cents=None refunds whatever is still
    unrefunded on the payment. Returns the provider's refund id."""
    remaining = order.amount_cents - (order.refunded_cents or 0)
    partial = amount_cents is not None and amount_cents < remaining
    if order.provider == "dodo":
        if partial or order.refunded_cents:
            raise RefundError("Partial refunds through Dodo Payments aren't automated. Refund it in the Dodo dashboard.")
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
            {"amount": amount_cents if partial else remaining, "notes": {"reason": reason[:200], "order": str(order.id)}},
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


async def refund_campaign_unsent(db: Session, campaign_id: int, *, reported_on, actor: str, reason: str) -> dict:
    """Refund Policy v3.0 §3.3: refund the credits a failed campaign never sent.

    Recomputes the §3.3 check server-side and refuses unless conditions 1, 2,
    3, 5 and 6 all hold (condition 4, "our fault", is the admin's call, which
    they make by pressing the button). Then, in order:
      1. refunds unsent credits x price paid per credit at the provider;
      2. cancels the campaign, which releases its unsent reserved credits;
      3. revokes those credits, so the money and the credits aren't both kept;
      4. records the amount on the payment (status stays 'paid' until the
         whole payment has been refunded).
    """
    from services.email_campaign.campaign_worker import finish_campaign
    from services.refund_check import campaign_refund_check

    check = campaign_refund_check(db, campaign_id, reported_on=reported_on)
    if check["verdict"] != "met":
        failing = [c["label"] for c in check["conditions"] if c["n"] != 4 and c["result"] != "yes"]
        raise RefundError("Refund Policy §3.3 is not met: " + "; ".join(failing))
    info = check["refund"]
    if not info or info["amount_cents"] <= 0:
        raise RefundError("No paid, unrefunded payment is linked to this campaign.")
    order = db.get(PaymentOrder, info["payment_id"])
    if order.status == "refunded":
        raise RefundError("This payment is already refunded.")

    refund_id = await _provider_refund(order, reason, amount_cents=info["amount_cents"])

    campaign = db.get(Campaign, campaign_id)
    if campaign.status in ("draft", "running", "paused"):
        finish_campaign(db, campaign, reason=f"§3.3 refund of payment {order.id}", final_status="cancelled")
    # finish_campaign only releases credits behind queued email rows. A
    # stalled campaign can still hold reservations with no row behind them,
    # so hand back whatever it holds beyond what was actually sent.
    still_held = ((campaign.credits_reserved or 0) - (campaign.credits_released or 0)
                  - check["campaign"]["first_touch_sent"])
    if still_held > 0:
        credits.release(db, order.user_id, still_held, credits.RELEASE_ADMIN, campaign=campaign,
                        actor=actor, note=f"§3.3 refund of payment {order.id}")
    revoked = credits.revoke(db, order.user_id, info["unsent_credits"], credits.REVOKE_REFUND,
                             payment_order_id=order.id, actor=actor, note=reason[:200])

    now = datetime.utcnow()
    order.refunded_cents = (order.refunded_cents or 0) + info["amount_cents"]
    order.refunded_at = now
    order.refund_id = refund_id
    order.updated_at = now
    if order.refunded_cents >= order.amount_cents:
        order.status = "refunded"
    db.commit()
    logger.info("[REFUND] campaign %s: %s cents of payment %s refunded by %s (§3.3), %s credits revoked, refund_id=%s",
                campaign_id, info["amount_cents"], order.id, actor, revoked, refund_id)
    return {
        "campaign_id": campaign_id, "payment_id": order.id, "refunded_cents": info["amount_cents"],
        "currency": order.currency, "refund_id": refund_id, "credits_revoked": revoked,
        "payment_status": order.status,
    }
