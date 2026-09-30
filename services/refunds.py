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
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.logger import get_logger
from database.models import Campaign, Candidate, PaymentOrder, PaymentRefund
from services import credits

logger = get_logger(__name__)


class RefundError(Exception):
    pass


async def _dodo_partial_refund(client, order: PaymentOrder, amount_cents: int, reason: str):
    """Dodo refunds part of a payment per line item: refunds.create(items=[{
    item_id, amount}]). Our checkouts have one product per payment; the line
    item says how much of it is still refundable, in the payment's currency."""
    lines = await client.payments.retrieve_line_items(order.dodo_payment_id)
    if str(lines.currency).upper() != (order.currency or "").upper():
        raise RefundError(f"Dodo charged this payment in {lines.currency}, not {order.currency}. "
                          "Refund it in the Dodo dashboard.")
    items = [i for i in lines.items if (i.refundable_amount or 0) > 0]
    if len(items) != 1:
        raise RefundError(f"This Dodo payment has {len(items)} refundable line items, not one. "
                          "Refund it in the Dodo dashboard.")
    item = items[0]
    if amount_cents > item.refundable_amount:
        raise RefundError(f"Dodo can refund at most {item.refundable_amount} more on this payment.")
    return await client.refunds.create(
        payment_id=order.dodo_payment_id,
        items=[{"item_id": item.items_id, "amount": amount_cents, "tax_inclusive": True}],
        reason=reason[:200],
    )


async def _provider_refund(order: PaymentOrder, reason: str, amount_cents: int | None = None) -> str:
    """Refund at the provider. amount_cents=None refunds whatever is still
    unrefunded on the payment. Returns the provider's refund id."""
    remaining = order.amount_cents - (order.refunded_cents or 0)
    partial = amount_cents is not None and amount_cents < remaining
    if order.provider == "dodo":
        if not order.dodo_payment_id:
            raise RefundError("This Dodo order has no payment id to refund.")
        from services.dodo_payments import _get_client
        client = _get_client()
        if partial or order.refunded_cents:
            refund = await _dodo_partial_refund(client, order, amount_cents if partial else remaining, reason)
        else:
            refund = await client.refunds.create(payment_id=order.dodo_payment_id, reason=reason[:200])
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


# A refund is claimed before the provider is asked (status 'refunding',
# committed), so a double click cannot refund twice and the provider's own
# refund webhook, which can land before we have settled, waits instead of
# settling the same refund a second time. No row lock is held across the
# provider call (the PP-P15 stall). A claim older than this is a crashed run.
CLAIM_STALE_AFTER = timedelta(minutes=10)
REFUNDABLE = ("paid", "completed")


class RefundInFlight(Exception):
    """A refund of this payment is being made right now; try again shortly."""


def _claim(db: Session, order: PaymentOrder) -> str:
    """Move the order to 'refunding' if it is still refundable. Returns the
    status to restore if the provider refuses."""
    prev = order.status
    if prev == "refunding" and order.updated_at and datetime.utcnow() - order.updated_at < CLAIM_STALE_AFTER:
        raise RefundError("A refund of this payment is already in progress.")
    if prev == "refunded":
        raise RefundError("This payment is already refunded.")
    if prev == "refunding":
        prev = "paid"  # a crashed run's claim; the payment itself was paid
    elif prev not in REFUNDABLE or not order.amount_cents or order.amount_cents <= 0:
        raise RefundError("Only a paid, real-money payment can be refunded.")
    claimed = (
        db.query(PaymentOrder)
        .filter(PaymentOrder.id == order.id, PaymentOrder.status == order.status)
        .update({"status": "refunding", "updated_at": datetime.utcnow()}, synchronize_session=False)
    )
    db.commit()
    if not claimed:
        raise RefundError("A refund of this payment is already in progress.")
    db.refresh(order)
    return prev


def _release_claim(db: Session, order: PaymentOrder, prev: str) -> None:
    db.rollback()
    db.query(PaymentOrder).filter(PaymentOrder.id == order.id, PaymentOrder.status == "refunding") \
        .update({"status": prev, "updated_at": datetime.utcnow()}, synchronize_session=False)
    db.commit()


def _credits_already_revoked(db: Session, order_id: int) -> int:
    return int(db.query(func.coalesce(func.sum(PaymentRefund.credits_revoked), 0))
               .filter(PaymentRefund.payment_order_id == order_id).scalar() or 0)


def _cancel_live_campaigns(db: Session, user_id: str, why: str) -> list[int]:
    from services.email_campaign.campaign_worker import finish_campaign
    live = (
        db.query(Campaign).join(Candidate, Candidate.id == Campaign.candidate_id)
        .filter(Candidate.user_id == user_id,
                Campaign.status.in_(("draft", "running", "paused")))
        .all()
    )
    for c in live:
        finish_campaign(db, c, reason=why, final_status="cancelled")
    return [c.id for c in live]


async def refund_payment(db: Session, order_id: int, *, actor: str, reason: str) -> dict:
    order = db.get(PaymentOrder, order_id)
    if order is None:
        raise RefundError("Payment not found.")
    prev = _claim(db, order)
    remaining = order.amount_cents - (order.refunded_cents or 0)

    # 2. The provider first: if it refuses, nothing in the app changes.
    try:
        refund_id = await _provider_refund(order, reason)
    except Exception:
        _release_claim(db, order, prev)
        raise

    # 3. Money is back, so stop the service: unfinished campaigns are cancelled
    #    (their unsent work retired, their credits released through the ledger).
    cancelled = _cancel_live_campaigns(db, order.user_id, f"payment {order.id} refunded")

    # 4. Settle.
    now = datetime.utcnow()
    order.status = "refunded"
    order.refunded_cents = order.amount_cents
    order.refunded_at = now
    order.refund_id = refund_id
    order.updated_at = now
    bought = max(0, (order.credits_granted or 0) - _credits_already_revoked(db, order.id))
    revoked = credits.revoke(db, order.user_id, bought, credits.REVOKE_REFUND,
                             payment_order_id=order.id, actor=actor, note=reason[:200])
    db.add(PaymentRefund(payment_order_id=order.id, provider=order.provider,
                         provider_refund_id=refund_id or f"order-{order.id}-{now.isoformat()}",
                         amount_cents=remaining, currency=order.currency, source="admin",
                         actor=actor, reason=reason[:500], credits_revoked=revoked, created_at=now))
    db.commit()
    logger.info("[REFUND] order %s refunded by %s: %s cents, %s of %s credits revoked, refund_id=%s",
                order.id, actor, remaining, revoked, bought, refund_id)
    return {
        "order_id": order.id,
        "status": "refunded",
        "refunded_cents": remaining,
        "currency": order.currency,
        "refund_id": refund_id,
        "campaigns_cancelled": cancelled,
        "credits_revoked": revoked,
        "credits_already_used": bought - revoked,
    }


def apply_provider_refund(db: Session, *, provider: str, payment_id: str, refund_id: str,
                          amount_cents: int | None, currency: str | None = None) -> str:
    """Settle a refund the provider reports by webhook (PP-P05).

    Refunds we made ourselves are already recorded under their provider id, so
    their webhook is a no-op. Anything else was refunded outside the app (the
    Razorpay or Dodo dashboard), and used to leave the credits granted and the
    payment counted as revenue. It is settled here the way the Refund button
    settles: a full refund cancels unfinished campaigns and revokes the
    credits the payment bought; a partial one revokes the same share of them.

    Returns 'duplicate', 'unknown' (no such payment), or 'settled'. Raises
    RefundInFlight while our own refund of this payment is mid-way.
    """
    if not refund_id or not payment_id:
        return "unknown"
    if db.query(PaymentRefund.id).filter(PaymentRefund.provider_refund_id == refund_id).first():
        return "duplicate"
    col = PaymentOrder.razorpay_payment_id if provider == "razorpay" else PaymentOrder.dodo_payment_id
    order = db.query(PaymentOrder).filter(col == payment_id).with_for_update().first()
    if order is None:
        db.rollback()
        logger.warning("[REFUND_WEBHOOK] %s refund %s for unknown payment %s", provider, refund_id, payment_id)
        return "unknown"
    now = datetime.utcnow()
    if order.status == "refunding" and order.updated_at and now - order.updated_at < CLAIM_STALE_AFTER:
        db.rollback()
        raise RefundInFlight(f"payment {order.id} is being refunded")
    already = order.refunded_cents or 0
    amount = amount_cents if amount_cents is not None else order.amount_cents - already
    amount = max(0, min(amount, order.amount_cents - already))
    full = already + amount >= order.amount_cents

    cancelled = _cancel_live_campaigns(db, order.user_id, f"payment {order.id} refunded at {provider}") if full else []
    prior = _credits_already_revoked(db, order.id)
    granted = order.credits_granted or 0
    if full:
        owed = granted - prior
    else:
        owed = (granted * amount) // order.amount_cents if order.amount_cents else 0
    revoked = credits.revoke(db, order.user_id, max(0, owed), credits.REVOKE_REFUND,
                             payment_order_id=order.id, actor=f"{provider}-webhook",
                             note=f"refund {refund_id} made outside the app")
    order.refunded_cents = already + amount
    order.refunded_at = now
    order.refund_id = refund_id
    order.updated_at = now
    order.status = "refunded" if full else ("paid" if order.status == "refunding" else order.status)
    db.add(PaymentRefund(payment_order_id=order.id, provider=provider, provider_refund_id=refund_id,
                         amount_cents=amount, currency=currency or order.currency, source="webhook",
                         actor=f"{provider}-webhook", reason="refunded outside the app",
                         credits_revoked=revoked, created_at=now))
    db.commit()
    logger.warning("[REFUND_WEBHOOK] settled %s refund %s on payment %s: %s cents (%s), %s credits revoked, "
                   "campaigns cancelled %s", provider, refund_id, order.id, amount,
                   "full" if full else "partial", revoked, cancelled)
    try:
        from services.reconcile import _tell_founders
        _tell_founders("refund made outside the app was settled",
                       f"Payment {order.id} ({provider}) was refunded at the provider: {amount} "
                       f"{order.currency} ({'full' if full else 'partial'}). {revoked} credits revoked; "
                       f"campaigns cancelled: {cancelled or 'none'}. Use the admin Refund button next time "
                       "so the reason is recorded.")
    except Exception:
        logger.exception("[REFUND_WEBHOOK] founder alert failed for payment %s", order.id)
    return "settled"


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
    prev = _claim(db, order)

    try:
        refund_id = await _provider_refund(order, reason, amount_cents=info["amount_cents"])
    except Exception:
        _release_claim(db, order, prev)
        raise

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
    order.status = "refunded" if order.refunded_cents >= order.amount_cents else prev
    db.add(PaymentRefund(payment_order_id=order.id, provider=order.provider,
                         provider_refund_id=refund_id or f"campaign-{campaign_id}-{now.isoformat()}",
                         amount_cents=info["amount_cents"], currency=order.currency, source="policy_3_3",
                         actor=actor, reason=reason[:500], credits_revoked=revoked, created_at=now))
    db.commit()
    logger.info("[REFUND] campaign %s: %s cents of payment %s refunded by %s (§3.3), %s credits revoked, refund_id=%s",
                campaign_id, info["amount_cents"], order.id, actor, revoked, refund_id)
    return {
        "campaign_id": campaign_id, "payment_id": order.id, "refunded_cents": info["amount_cents"],
        "currency": order.currency, "refund_id": refund_id, "credits_revoked": revoked,
        "payment_status": order.status,
    }
