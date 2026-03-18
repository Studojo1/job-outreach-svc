"""Payment Routes — Razorpay order creation, verification, webhooks, and credits."""

import hashlib
import hmac
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
from core.pricing import get_tier_pricing, apply_coupon, TIERS, TEST_TIERS
from api.dependencies import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payment", tags=["Payment"])


def _get_razorpay_client():
    import razorpay
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


# ── Pricing Info ──────────────────────────────────────────────────────────────

@router.get("/pricing")
async def get_pricing(currency: str = "USD"):
    """Return tier pricing for the given currency."""
    source = TEST_TIERS if settings.RAZORPAY_TEST_MODE else TIERS
    result = []
    for tier_val, tp in source.items():
        if tier_val == 5:
            continue  # free test tier not shown in pricing
        amount = tp.price_inr if currency.upper() == "INR" else tp.price_usd
        result.append({
            "tier": tp.tier,
            "label": tp.label,
            "amount_cents": amount,
            "currency": currency.upper(),
            "display_price": f"{'₹' if currency.upper() == 'INR' else '$'}{amount / 100:.0f}",
        })
    return {"tiers": result, "test_mode": settings.RAZORPAY_TEST_MODE}


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


# ── Create Razorpay Order ────────────────────────────────────────────────────

class CreateOrderRequest(BaseModel):
    tier: int
    currency: str = "USD"
    coupon_code: Optional[str] = None


@router.post("/create-order")
async def create_order(
    request: CreateOrderRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a Razorpay order for the selected tier."""
    if request.tier == 5:
        raise HTTPException(status_code=400, detail="Test tier is free — no payment needed")

    pricing = get_tier_pricing(request.tier, settings.RAZORPAY_TEST_MODE)
    currency = request.currency.upper()
    amount = pricing.price_inr if currency == "INR" else pricing.price_usd

    # Apply coupon if provided
    coupon_id = None
    if request.coupon_code:
        coupon = db.query(Coupon).filter(
            Coupon.code == request.coupon_code.strip().upper(),
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

    if amount <= 0:
        # Fully discounted — grant credits directly
        idem_key = str(uuid.uuid4())
        order = PaymentOrder(
            user_id=current_user.id,
            amount_cents=0,
            currency=currency,
            tier=request.tier,
            coupon_id=coupon_id,
            status="paid",
            credits_granted=request.tier,
            idempotency_key=idem_key,
        )
        db.add(order)
        _grant_credits(db, current_user.id, request.tier)
        if coupon_id:
            db.query(Coupon).filter_by(id=coupon_id).update({"uses": Coupon.uses + 1})
        db.commit()
        logger.info("[PAYMENT] Free order (100%% coupon) for user %s, tier %d", current_user.id, request.tier)
        return {"free": True, "credits_granted": request.tier}

    # Create Razorpay order
    client = _get_razorpay_client()
    idem_key = str(uuid.uuid4())

    try:
        rz_order = client.order.create({
            "amount": amount,
            "currency": currency,
            "receipt": f"order_{idem_key[:8]}",
            "notes": {
                "user_id": str(current_user.id),
                "tier": str(request.tier),
                "coupon_id": str(coupon_id) if coupon_id else "",
            },
        })
    except Exception as e:
        logger.error("[PAYMENT] Razorpay order creation failed: %s", e)
        raise HTTPException(status_code=502, detail="Payment gateway error. Please try again.")

    # Store order in DB
    order = PaymentOrder(
        user_id=current_user.id,
        razorpay_order_id=rz_order["id"],
        amount_cents=amount,
        currency=currency,
        tier=request.tier,
        coupon_id=coupon_id,
        status="created",
        idempotency_key=idem_key,
    )
    db.add(order)
    db.commit()

    logger.info("[PAYMENT] Order created: %s for user %s, tier %d, amount %d %s",
                rz_order["id"], current_user.id, request.tier, amount, currency)

    return {
        "order_id": rz_order["id"],
        "amount": amount,
        "currency": currency,
        "key_id": settings.RAZORPAY_KEY_ID,
        "tier": request.tier,
    }


# ── Verify Payment ───────────────────────────────────────────────────────────

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

    return {"status": "verified", "credits": order.credits_granted}


# ── Webhook (server-to-server from Razorpay) ─────────────────────────────────

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

    import json
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
