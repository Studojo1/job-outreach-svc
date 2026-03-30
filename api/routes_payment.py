"""Payment Routes — Razorpay + Dodo Payments with geo-based routing."""

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from database.session import get_db
from database.models import User, Coupon, PaymentOrder, UserCredit
from core.config import settings
from core.pricing import get_tier_pricing, get_dodo_product_id, apply_coupon, TIERS, TEST_TIERS
from core.geo import detect_country, is_india
from api.dependencies import get_current_user
from core.analytics import capture
import services.dodo_payments as dodo_svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payment", tags=["Payment"])


def _get_razorpay_client():
    import razorpay
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


# ── Pricing Info ──────────────────────────────────────────────────────────────

@router.get("/pricing")
async def get_pricing(req: Request):
    """Return tier pricing — currency auto-detected from geo (India → INR, else USD)."""
    currency = "INR" if is_india(req) else "USD"

    source = TEST_TIERS if settings.RAZORPAY_TEST_MODE else TIERS
    result = []
    for tier_val, tp in source.items():
        if tier_val == 5:
            continue  # free test tier not shown in pricing
        amount = tp.price_inr if currency == "INR" else tp.price_usd
        result.append({
            "tier": tp.tier,
            "label": tp.label,
            "amount_cents": amount,
            "currency": currency,
            "display_price": f"{'₹' if currency == 'INR' else '$'}{amount / 100:.0f}",
        })
    return {"tiers": result, "test_mode": settings.RAZORPAY_TEST_MODE, "currency": currency}


# ── Coupon Validation ─────────────────────────────────────────────────────────

class CouponCheckRequest(BaseModel):
    code: str
    tier: int
    currency: str = "USD"


@router.post("/coupon/validate")
async def validate_coupon(
    request: CouponCheckRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Validate a coupon code and return the discounted price."""
    coupon = db.query(Coupon).filter(
        Coupon.code == request.code.strip().upper(),
        Coupon.is_active == True,
    ).first()

    if not coupon:
        raise HTTPException(status_code=404, detail="Invalid coupon code")

    now = datetime.utcnow()
    if coupon.valid_until and coupon.valid_until < now:
        raise HTTPException(status_code=400, detail="Coupon has expired")
    if coupon.valid_from and coupon.valid_from > now:
        raise HTTPException(status_code=400, detail="Coupon is not yet active")
    if coupon.max_uses is not None and coupon.uses >= coupon.max_uses:
        raise HTTPException(status_code=400, detail="Coupon usage limit reached")

    pricing = get_tier_pricing(request.tier, settings.RAZORPAY_TEST_MODE)
    original = pricing.price_inr if request.currency.upper() == "INR" else pricing.price_usd
    discounted = apply_coupon(original, coupon.discount_type, float(coupon.discount_value))

    return {
        "valid": True,
        "coupon_id": coupon.id,
        "discount_type": coupon.discount_type,
        "discount_value": float(coupon.discount_value),
        "original_amount": original,
        "discounted_amount": discounted,
        "currency": request.currency.upper(),
        "distributor": coupon.distributor_name,
    }


# ── Create Payment Order (geo-routed) ────────────────────────────────────────

class CreateOrderRequest(BaseModel):
    tier: int
    currency: str = "USD"
    coupon_code: Optional[str] = None


@router.post("/create-order")
async def create_order(
    body: CreateOrderRequest,
    req: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a payment order — routes to Razorpay (India) or Dodo (international)."""
    if body.tier == 5:
        raise HTTPException(status_code=400, detail="Test tier is free — no payment needed")

    # Detect geo for gateway routing
    country = detect_country(req)
    use_razorpay = is_india(req)

    # Determine currency and amount based on gateway
    pricing = get_tier_pricing(body.tier, settings.RAZORPAY_TEST_MODE)
    if use_razorpay:
        currency = "INR"
        amount = pricing.price_inr
    else:
        currency = "USD"
        amount = pricing.price_usd

    # Apply coupon if provided
    coupon_id = None
    if body.coupon_code:
        coupon = db.query(Coupon).filter(
            Coupon.code == body.coupon_code.strip().upper(),
            Coupon.is_active == True,
        ).first()
        if coupon:
            now = datetime.utcnow()
            valid = True
            if coupon.valid_until and coupon.valid_until < now:
                valid = False
            if coupon.max_uses is not None and coupon.uses >= coupon.max_uses:
                valid = False
            if valid:
                amount = apply_coupon(amount, coupon.discount_type, float(coupon.discount_value))
                coupon_id = coupon.id
                capture("coupon_applied", str(current_user.id), {
                    "coupon_code": body.coupon_code.strip().upper(),
                    "discount_type": coupon.discount_type,
                    "discount_value": float(coupon.discount_value),
                    "tier": body.tier,
                })

    if amount <= 0:
        # Fully discounted — grant credits directly
        idem_key = str(uuid.uuid4())
        order = PaymentOrder(
            user_id=current_user.id,
            provider="coupon",
            amount_cents=0,
            currency=currency,
            tier=body.tier,
            coupon_id=coupon_id,
            geo_country=country,
            status="paid",
            credits_granted=body.tier,
            idempotency_key=idem_key,
        )
        db.add(order)
        _grant_credits(db, current_user.id, body.tier)
        if coupon_id:
            db.query(Coupon).filter_by(id=coupon_id).update({"uses": Coupon.uses + 1})
        db.commit()
        logger.info("[PAYMENT] Free order (100%% coupon) for user %s, tier %d", current_user.id, body.tier)
        capture("payment_confirmed", str(current_user.id), {
            "tier": body.tier,
            "credits_granted": body.tier,
            "provider": "coupon",
            "amount_cents": 0,
            "currency": currency,
            "country": country,
        })
        return {"free": True, "credits_granted": body.tier}

    idem_key = str(uuid.uuid4())

    # ── Dodo Payments (international) ──────────────────────────────────────
    if not use_razorpay:
        try:
            product_id = get_dodo_product_id(settings)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # Redirect back to enrichment page with dodo_return flag.
        # The enrichment page handles verification inline (like Razorpay modal).
        return_url = f"{settings.FRONTEND_URL}/enrichment?dodo_return=1"

        try:
            dodo_result = await dodo_svc.create_checkout(
                product_id=product_id,
                customer_email=current_user.email,
                customer_name=current_user.name or "Customer",
                return_url=return_url,
                amount_cents=amount,
                metadata={
                    "user_id": str(current_user.id),
                    "tier": str(body.tier),
                    "coupon_id": str(coupon_id) if coupon_id else "",
                    "idempotency_key": idem_key,
                },
            )
        except Exception as e:
            logger.error("[PAYMENT] Dodo checkout creation failed: %s", e)
            raise HTTPException(status_code=502, detail="Payment gateway error. Please try again.")

        order = PaymentOrder(
            user_id=current_user.id,
            provider="dodo",
            dodo_checkout_id=dodo_result["session_id"],
            amount_cents=amount,
            currency=currency,
            tier=body.tier,
            coupon_id=coupon_id,
            geo_country=country,
            status="created",
            idempotency_key=idem_key,
        )
        db.add(order)
        db.commit()

        logger.info("[PAYMENT] Dodo order created: %s for user %s, tier %d, amount %d %s",
                    dodo_result["session_id"], current_user.id, body.tier, amount, currency)
        capture("payment_order_created", str(current_user.id), {
            "tier": body.tier,
            "amount_cents": amount,
            "currency": currency,
            "provider": "dodo",
            "coupon_applied": coupon_id is not None,
            "country": country,
        })

        return {
            "provider": "dodo",
            "checkout_url": dodo_result["checkout_url"],
            "session_id": dodo_result["session_id"],
            "tier": body.tier,
            "dodo_test_mode": settings.DODO_TEST_MODE,
        }

    # ── Razorpay (India) ──────────────────────────────────────────────────
    client = _get_razorpay_client()

    try:
        rz_order = client.order.create({
            "amount": amount,
            "currency": currency,
            "receipt": f"order_{idem_key[:8]}",
            "notes": {
                "user_id": str(current_user.id),
                "tier": str(body.tier),
                "coupon_id": str(coupon_id) if coupon_id else "",
            },
        })
    except Exception as e:
        logger.error("[PAYMENT] Razorpay order creation failed: %s", e)
        raise HTTPException(status_code=502, detail="Payment gateway error. Please try again.")

    order = PaymentOrder(
        user_id=current_user.id,
        provider="razorpay",
        razorpay_order_id=rz_order["id"],
        amount_cents=amount,
        currency=currency,
        tier=body.tier,
        coupon_id=coupon_id,
        geo_country=country,
        status="created",
        idempotency_key=idem_key,
    )
    db.add(order)
    db.commit()

    logger.info("[PAYMENT] Razorpay order created: %s for user %s, tier %d, amount %d %s",
                rz_order["id"], current_user.id, body.tier, amount, currency)
    capture("payment_order_created", str(current_user.id), {
        "tier": body.tier,
        "amount_cents": amount,
        "currency": currency,
        "provider": "razorpay",
        "coupon_applied": coupon_id is not None,
        "country": country,
    })

    return {
        "provider": "razorpay",
        "order_id": rz_order["id"],
        "amount": amount,
        "currency": currency,
        "key_id": settings.RAZORPAY_KEY_ID,
        "tier": body.tier,
    }


# ── Verify Razorpay Payment ─────────────────────────────────────────────────

class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


@router.post("/verify")
async def verify_payment(
    request: VerifyPaymentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Verify Razorpay payment signature and grant credits."""
    order = db.query(PaymentOrder).filter_by(
        razorpay_order_id=request.razorpay_order_id,
        user_id=current_user.id,
    ).first()

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.status == "paid":
        return {"status": "already_verified", "credits": order.credits_granted}

    # Verify signature
    expected_signature = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode(),
        f"{request.razorpay_order_id}|{request.razorpay_payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_signature, request.razorpay_signature):
        order.status = "failed"
        db.commit()
        logger.error("[PAYMENT] Signature mismatch for order %s", request.razorpay_order_id)
        raise HTTPException(status_code=400, detail="Payment verification failed")

    # Mark paid and grant credits (idempotent)
    order.razorpay_payment_id = request.razorpay_payment_id
    order.razorpay_signature = request.razorpay_signature
    order.status = "paid"
    order.credits_granted = order.tier
    order.updated_at = datetime.utcnow()

    _grant_credits(db, current_user.id, order.tier)

    # Increment coupon usage
    if order.coupon_id:
        db.query(Coupon).filter_by(id=order.coupon_id).update({"uses": Coupon.uses + 1})

    db.commit()

    logger.info("[PAYMENT] Payment verified: %s, granted %d credits to user %s",
                request.razorpay_order_id, order.tier, current_user.id)
    capture("payment_confirmed", str(current_user.id), {
        "tier": order.tier,
        "credits_granted": order.tier,
        "provider": "razorpay",
        "amount_cents": order.amount_cents,
        "currency": order.currency,
        "country": order.geo_country,
    })

    return {"status": "verified", "credits": order.credits_granted}


# ── Verify Dodo Payment (frontend polls after redirect) ──────────────────────

class VerifyDodoRequest(BaseModel):
    session_id: str


@router.post("/verify-dodo")
async def verify_dodo_payment(
    request: VerifyDodoRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Check if a Dodo payment has been confirmed. Actively checks Dodo API if still pending."""
    order = db.query(PaymentOrder).filter_by(
        dodo_checkout_id=request.session_id,
        user_id=current_user.id,
    ).first()

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.status == "paid":
        return {"status": "paid", "credits": order.credits_granted, "tier": order.tier}

    if order.status == "failed":
        return {"status": "failed"}

    # Order still pending — actively check Dodo API instead of waiting for webhook
    dodo_status = await dodo_svc.get_checkout_status(request.session_id)
    logger.info("[PAYMENT] Dodo checkout %s status from API: %s", request.session_id, dodo_status)

    if dodo_status["status"] in ("succeeded", "paid", "complete", "completed"):
        # Payment confirmed — grant credits
        order.dodo_payment_id = dodo_status.get("payment_id", "")
        order.status = "paid"
        order.credits_granted = order.tier
        order.updated_at = datetime.utcnow()
        _grant_credits(db, order.user_id, order.tier)

        if order.coupon_id:
            db.query(Coupon).filter_by(id=order.coupon_id).update({"uses": Coupon.uses + 1})

        db.commit()
        logger.info("[PAYMENT] Dodo payment verified via API: checkout=%s, granted %d credits",
                    request.session_id, order.tier)
        capture("payment_confirmed", str(order.user_id), {
            "tier": order.tier,
            "credits_granted": order.tier,
            "provider": "dodo",
            "amount_cents": order.amount_cents,
            "currency": order.currency,
            "country": order.geo_country,
        })
        return {"status": "paid", "credits": order.credits_granted, "tier": order.tier}

    if dodo_status["status"] in ("failed", "expired", "cancelled"):
        order.status = "failed"
        order.updated_at = datetime.utcnow()
        db.commit()
        return {"status": "failed"}

    return {"status": "pending"}


# ── Dodo Webhook (server-to-server) ──────────────────────────────────────────

@router.post("/webhook/dodo")
async def dodo_webhook(request: Request, db: Session = Depends(get_db)):
    """Dodo Payments webhook handler. Verifies Standard Webhooks signature."""
    body = await request.body()

    # Verify Standard Webhooks signature if secret is configured
    if settings.DODO_WEBHOOK_SECRET:
        try:
            from standardwebhooks.webhooks import Webhook
            wh = Webhook(settings.DODO_WEBHOOK_SECRET)
            wh.verify(
                body.decode(),
                {
                    "webhook-id": request.headers.get("webhook-id", ""),
                    "webhook-timestamp": request.headers.get("webhook-timestamp", ""),
                    "webhook-signature": request.headers.get("webhook-signature", ""),
                },
            )
        except Exception as e:
            logger.error("[DODO_WEBHOOK] Signature verification failed: %s", e)
            raise HTTPException(status_code=400, detail="Invalid webhook signature")

    payload = json.loads(body)
    event_type = payload.get("event_type") or payload.get("type", "")
    data = payload.get("data", {})

    logger.info("[DODO_WEBHOOK] Received event: %s", event_type)

    if event_type == "payment.succeeded":
        checkout_id = data.get("checkout_id") or data.get("metadata", {}).get("checkout_session_id", "")
        payment_id = data.get("payment_id", "")

        if not checkout_id:
            logger.warning("[DODO_WEBHOOK] payment.succeeded without checkout_id: %s", payload)
            return {"status": "ok"}

        order = db.query(PaymentOrder).filter_by(dodo_checkout_id=checkout_id).first()
        if not order:
            logger.warning("[DODO_WEBHOOK] No order found for checkout %s", checkout_id)
            return {"status": "ok"}

        # Idempotent — skip if already paid
        if order.status == "paid":
            logger.info("[DODO_WEBHOOK] Order already paid: %s", checkout_id)
            return {"status": "ok"}

        order.dodo_payment_id = payment_id
        order.status = "paid"
        order.credits_granted = order.tier
        order.updated_at = datetime.utcnow()

        _grant_credits(db, order.user_id, order.tier)

        if order.coupon_id:
            db.query(Coupon).filter_by(id=order.coupon_id).update({"uses": Coupon.uses + 1})

        db.commit()
        logger.info("[DODO_WEBHOOK] Payment succeeded: checkout=%s, granted %d credits to user %s",
                    checkout_id, order.tier, order.user_id)

    elif event_type == "payment.failed":
        checkout_id = data.get("checkout_id", "")
        if checkout_id:
            order = db.query(PaymentOrder).filter_by(dodo_checkout_id=checkout_id).first()
            if order and order.status == "created":
                order.status = "failed"
                order.updated_at = datetime.utcnow()
                db.commit()
                logger.warning("[DODO_WEBHOOK] Payment failed: %s", checkout_id)

    return {"status": "ok"}


# ── Razorpay Webhook (server-to-server) ──────────────────────────────────────

@router.post("/webhook")
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    """Razorpay webhook handler. Verifies signature from X-Razorpay-Signature header."""
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if settings.RAZORPAY_WEBHOOK_SECRET:
        expected = hmac.new(
            settings.RAZORPAY_WEBHOOK_SECRET.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected, signature):
            logger.error("[PAYMENT_WEBHOOK] Signature mismatch")
            raise HTTPException(status_code=400, detail="Invalid signature")

    payload = json.loads(body)
    event = payload.get("event", "")

    if event == "payment.captured":
        payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        rz_order_id = payment_entity.get("order_id")
        rz_payment_id = payment_entity.get("id")

        if rz_order_id:
            order = db.query(PaymentOrder).filter_by(razorpay_order_id=rz_order_id).first()
            if order and order.status != "paid":
                order.razorpay_payment_id = rz_payment_id
                order.status = "paid"
                order.credits_granted = order.tier
                order.updated_at = datetime.utcnow()
                _grant_credits(db, order.user_id, order.tier)
                if order.coupon_id:
                    db.query(Coupon).filter_by(id=order.coupon_id).update({"uses": Coupon.uses + 1})
                db.commit()
                logger.info("[PAYMENT_WEBHOOK] Payment captured: %s, granted %d credits", rz_order_id, order.tier)

    elif event == "payment.failed":
        payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        rz_order_id = payment_entity.get("order_id")
        if rz_order_id:
            order = db.query(PaymentOrder).filter_by(razorpay_order_id=rz_order_id).first()
            if order and order.status == "created":
                order.status = "failed"
                order.updated_at = datetime.utcnow()
                db.commit()
                logger.warning("[PAYMENT_WEBHOOK] Payment failed: %s", rz_order_id)

    return {"status": "ok"}


# ── Credits ───────────────────────────────────────────────────────────────────

@router.get("/credits")
async def get_credits(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the user's current credit balance."""
    credit = db.query(UserCredit).filter_by(user_id=current_user.id).first()
    if not credit:
        return {"total_credits": 0, "used_credits": 0, "available_credits": 0}
    return {
        "total_credits": credit.total_credits,
        "used_credits": credit.used_credits,
        "available_credits": credit.total_credits - credit.used_credits,
    }


def _grant_credits(db: Session, user_id: str, amount: int):
    """Add credits to user's balance. Creates row if not exists."""
    credit = db.query(UserCredit).filter_by(user_id=user_id).first()
    if credit:
        credit.total_credits += amount
        credit.updated_at = datetime.utcnow()
    else:
        credit = UserCredit(user_id=user_id, total_credits=amount)
        db.add(credit)


def deduct_credits(db: Session, user_id: str, amount: int) -> bool:
    """Deduct credits from user's balance. Returns False if insufficient."""
    credit = db.query(UserCredit).filter_by(user_id=user_id).first()
    if not credit or (credit.total_credits - credit.used_credits) < amount:
        return False
    credit.used_credits += amount
    credit.updated_at = datetime.utcnow()
    return True


def refund_credits(db: Session, user_id: str, amount: int):
    """Refund credits back to user's balance (e.g. enrichment failed)."""
    credit = db.query(UserCredit).filter_by(user_id=user_id).first()
    if credit and amount > 0:
        credit.used_credits = max(0, credit.used_credits - amount)
        credit.updated_at = datetime.utcnow()
        logger.info("[PAYMENT] Refunded %d credits to user %s", amount, user_id)