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
from database.models import User, Coupon, PaymentOrder, UserCredit, OutreachOrder
from core.config import settings
from core.pricing import (
    get_plan, get_plans, get_tier_pricing, get_dodo_product_id, apply_coupon,
    TIERS, TEST_TIERS,
)
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
    """Return all plan pricing — currency auto-detected from geo (India → INR, else USD)."""
    currency = "INR" if is_india(req) else "USD"
    sym = "₹" if currency == "INR" else "$"

    plans = get_plans(settings.RAZORPAY_TEST_MODE)
    result = []
    for p in plans:
        amount = p.price_inr if currency == "INR" else p.price_usd
        # Skip plans that have no price for this currency (e.g. email_50 is India-only)
        if amount == 0 and not (p.email_credits == 0 and p.linkedin_credits == 0):
            continue
        result.append({
            "plan_id": p.plan_id,
            "plan_type": p.plan_type,
            "label": p.label,
            "email_credits": p.email_credits,
            "linkedin_credits": p.linkedin_credits,
            "amount_cents": amount,
            "currency": currency,
            "display_price": f"{sym}{amount / 100:.0f}",
            "duration_days": p.duration_days,
        })
    # Legacy `tiers` shape — the pre-merge enrichment page reads this. Kept
    # alongside `plans` so both the old (email-only) and new (9-plan) frontends
    # render the right pricing in INR/USD without a frontend rebuild.
    #
    # Anchor pricing — show a struck-out "original" price next to the actual
    # price so customers see the discount visually. INR only for now; USD
    # users see flat pricing (no strikethrough). The anchor numbers and
    # resulting discount percentages were product-defined.
    ANCHOR_INR_PAISE = {200: 250000, 350: 350000, 500: 500000}  # ₹2500 / ₹3500 / ₹5000

    tiers = []
    for p in result:
        if p["plan_type"] != "email":
            continue
        tier_num = p["email_credits"]
        anchor_paise = ANCHOR_INR_PAISE.get(tier_num) if currency == "INR" else None
        discount_pct = None
        anchor_display = None
        if anchor_paise and anchor_paise > p["amount_cents"]:
            discount_pct = round((anchor_paise - p["amount_cents"]) / anchor_paise * 100)
            anchor_display = f"₹{anchor_paise // 100}"
        tiers.append({
            "tier": tier_num,
            "label": p["label"],
            "amount_cents": p["amount_cents"],
            "currency": p["currency"],
            "display_price": p["display_price"],
            "anchor_display": anchor_display,   # e.g. "₹2500", or null
            "discount_pct": discount_pct,        # e.g. 27, or null
            "duration_days": p["duration_days"], # 0 = unlimited
        })

    return {
        "plans": result,
        "tiers": tiers,
        "test_mode": settings.RAZORPAY_TEST_MODE,
        "currency": currency,
    }


# ── Coupon Validation ─────────────────────────────────────────────────────────

# ── Checkout failure diagnostics ──────────────────────────────────────────────
# In June 2026 we saw 20 customer orders created with razorpay_order_id but
# zero payment attempts on Razorpay's side — meaning the checkout modal never
# opened. This unauth'd endpoint just captures the client-side state so we can
# diagnose: missing global, constructor throw, open() throw, etc.

@router.post("/checkout-diag")
async def checkout_diagnostic(body: dict, req: Request):
    logger.warning(
        "[CHECKOUT-DIAG] stage=%s rzp_loaded=%s order=%s amount=%s plan=%s ua=%s err=%s",
        body.get("stage"),
        body.get("razorpay_loaded"),
        body.get("order_id"),
        body.get("amount"),
        body.get("plan_id"),
        (body.get("user_agent") or "")[:120],
        body.get("error"),
    )
    return {"ok": True}


class CouponCheckRequest(BaseModel):
    code: str
    tier: Optional[int] = None
    plan_id: Optional[str] = None
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

    # Support both plan_id (new) and tier (legacy)
    if request.plan_id:
        plan = get_plan(request.plan_id, settings.RAZORPAY_TEST_MODE)
        original = plan.price_inr if request.currency.upper() == "INR" else plan.price_usd
    elif request.tier:
        pricing = get_tier_pricing(request.tier, settings.RAZORPAY_TEST_MODE)
        original = pricing.price_inr if request.currency.upper() == "INR" else pricing.price_usd
    else:
        raise HTTPException(status_code=400, detail="plan_id or tier required")

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
    tier: Optional[int] = None        # legacy email-only path
    plan_id: Optional[str] = None     # new path: "email_200", "linkedin_350", "both_500", etc.
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
    # Resolve plan from plan_id (new) or tier (legacy email-only)
    if body.plan_id:
        if body.plan_id == "email_5":
            raise HTTPException(status_code=400, detail="Test tier is free — no payment needed")
        plan = get_plan(body.plan_id, settings.RAZORPAY_TEST_MODE)
        resolved_plan_id = body.plan_id
        resolved_tier = plan.email_credits if plan.email_credits else plan.linkedin_credits
    elif body.tier:
        if body.tier == 5:
            raise HTTPException(status_code=400, detail="Test tier is free — no payment needed")
        # Legacy: email-only
        from core.pricing import get_tier_pricing as _gtp
        _legacy = _gtp(body.tier, settings.RAZORPAY_TEST_MODE)
        # Map to a plan_id
        plan = get_plan(f"email_{body.tier}", settings.RAZORPAY_TEST_MODE)
        resolved_plan_id = plan.plan_id
        resolved_tier = body.tier
    else:
        raise HTTPException(status_code=400, detail="plan_id or tier required")

    # email_50 is India-only (no USD price); block non-India orders
    if resolved_plan_id == "email_50" and not is_india(req):
        raise HTTPException(status_code=400, detail="This plan is only available in India.")

    # Detect geo for gateway routing
    country = detect_country(req)
    use_razorpay = is_india(req)

    if use_razorpay:
        currency = "INR"
        amount = plan.price_inr
    else:
        currency = "USD"
        amount = plan.price_usd

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
                    "plan_id": resolved_plan_id,
                })

    if amount <= 0:
        # Fully discounted — grant credits directly
        idem_key = str(uuid.uuid4())
        from services.stage_tracking import safe_mark_stage, get_or_create_active_order
        try:
            outreach_order = get_or_create_active_order(db, str(current_user.id))
            outreach_order_id = outreach_order.id
        except Exception:
            outreach_order_id = None
        order = PaymentOrder(
            user_id=current_user.id,
            provider="coupon",
            amount_cents=0,
            currency=currency,
            tier=resolved_tier,
            plan_id=resolved_plan_id,
            coupon_id=coupon_id,
            outreach_order_id=outreach_order_id,
            geo_country=country,
            status="paid",
            credits_granted=plan.email_credits,
            idempotency_key=idem_key,
        )
        db.add(order)
        if plan.email_credits:
            _grant_credits(db, current_user.id, plan.email_credits)
        if coupon_id:
            db.query(Coupon).filter_by(id=coupon_id).update({"uses": Coupon.uses + 1})
        _set_plan_on_order(db, outreach_order_id, plan)
        db.commit()
        logger.info("[PAYMENT] Free order (100%% coupon) for user %s, plan %s", current_user.id, resolved_plan_id)
        capture("payment_confirmed", str(current_user.id), {
            "plan_id": resolved_plan_id,
            "plan_type": plan.plan_type,
            "email_credits": plan.email_credits,
            "linkedin_credits": plan.linkedin_credits,
            "provider": "coupon",
            "amount_cents": 0,
            "currency": currency,
            "country": country,
        })
        safe_mark_stage(db, str(current_user.id), "payment_page_reached")
        safe_mark_stage(db, str(current_user.id), "payment_made")
        return {"free": True, "credits_granted": plan.email_credits, "plan_id": resolved_plan_id, "plan_type": plan.plan_type}

    idem_key = str(uuid.uuid4())

    # ── Dodo Payments (international) ──────────────────────────────────────
    if not use_razorpay:
        try:
            product_id = get_dodo_product_id(settings)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

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
                    "plan_id": resolved_plan_id,
                    "tier": str(resolved_tier),
                    "coupon_id": str(coupon_id) if coupon_id else "",
                    "idempotency_key": idem_key,
                },
            )
        except Exception as e:
            logger.error("[PAYMENT] Dodo checkout creation failed: %s", e)
            raise HTTPException(status_code=502, detail="Payment gateway error. Please try again.")

        from services.stage_tracking import safe_mark_stage, get_or_create_active_order
        try:
            outreach_order = get_or_create_active_order(db, str(current_user.id))
            outreach_order_id = outreach_order.id
        except Exception:
            outreach_order_id = None
        order = PaymentOrder(
            user_id=current_user.id,
            provider="dodo",
            dodo_checkout_id=dodo_result["session_id"],
            amount_cents=amount,
            currency=currency,
            tier=resolved_tier,
            plan_id=resolved_plan_id,
            coupon_id=coupon_id,
            outreach_order_id=outreach_order_id,
            geo_country=country,
            status="created",
            idempotency_key=idem_key,
        )
        db.add(order)
        _set_plan_on_order(db, outreach_order_id, plan)
        db.commit()

        logger.info("[PAYMENT] Dodo order created: %s for user %s, plan %s, amount %d %s",
                    dodo_result["session_id"], current_user.id, resolved_plan_id, amount, currency)
        capture("payment_order_created", str(current_user.id), {
            "plan_id": resolved_plan_id,
            "plan_type": plan.plan_type,
            "amount_cents": amount,
            "currency": currency,
            "provider": "dodo",
            "coupon_applied": coupon_id is not None,
            "country": country,
        })
        safe_mark_stage(db, str(current_user.id), "payment_page_reached")

        return {
            "provider": "dodo",
            "checkout_url": dodo_result["checkout_url"],
            "session_id": dodo_result["session_id"],
            "plan_id": resolved_plan_id,
            "plan_type": plan.plan_type,
            "tier": resolved_tier,
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
                "plan_id": resolved_plan_id,
                "tier": str(resolved_tier),
                "coupon_id": str(coupon_id) if coupon_id else "",
            },
        })
    except Exception as e:
        logger.error("[PAYMENT] Razorpay order creation failed: %s", e)
        raise HTTPException(status_code=502, detail="Payment gateway error. Please try again.")

    from services.stage_tracking import safe_mark_stage, get_or_create_active_order
    try:
        outreach_order = get_or_create_active_order(db, str(current_user.id))
        outreach_order_id = outreach_order.id
    except Exception:
        outreach_order_id = None
    order = PaymentOrder(
        user_id=current_user.id,
        provider="razorpay",
        razorpay_order_id=rz_order["id"],
        amount_cents=amount,
        currency=currency,
        tier=resolved_tier,
        plan_id=resolved_plan_id,
        coupon_id=coupon_id,
        outreach_order_id=outreach_order_id,
        geo_country=country,
        status="created",
        idempotency_key=idem_key,
    )
    db.add(order)
    _set_plan_on_order(db, outreach_order_id, plan)
    db.commit()

    logger.info("[PAYMENT] Razorpay order created: %s for user %s, plan %s, amount %d %s",
                rz_order["id"], current_user.id, resolved_plan_id, amount, currency)
    capture("payment_order_created", str(current_user.id), {
        "plan_id": resolved_plan_id,
        "plan_type": plan.plan_type,
        "amount_cents": amount,
        "currency": currency,
        "provider": "razorpay",
        "coupon_applied": coupon_id is not None,
        "country": country,
    })
    safe_mark_stage(db, str(current_user.id), "payment_page_reached")

    return {
        "provider": "razorpay",
        "order_id": rz_order["id"],
        "amount": amount,
        "currency": currency,
        "key_id": settings.RAZORPAY_KEY_ID,
        "plan_id": resolved_plan_id,
        "plan_type": plan.plan_type,
        "tier": resolved_tier,
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
        return {"status": "already_verified", "credits": order.credits_granted, "plan_type": _order_plan_type(order)}

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

    order.razorpay_payment_id = request.razorpay_payment_id
    order.razorpay_signature = request.razorpay_signature
    order.status = "paid"
    order.updated_at = datetime.utcnow()

    _finalize_credits(db, order)

    if order.coupon_id:
        db.query(Coupon).filter_by(id=order.coupon_id).update({"uses": Coupon.uses + 1})

    db.commit()

    logger.info("[PAYMENT] Payment verified: %s, plan=%s, user %s",
                request.razorpay_order_id, order.plan_id, current_user.id)
    capture("payment_confirmed", str(current_user.id), {
        "plan_id": order.plan_id,
        "plan_type": _order_plan_type(order),
        "credits_granted": order.credits_granted,
        "provider": "razorpay",
        "amount_cents": order.amount_cents,
        "currency": order.currency,
        "country": order.geo_country,
    })

    from services.stage_tracking import safe_mark_stage
    safe_mark_stage(db, str(current_user.id), "payment_made")

    return {"status": "verified", "credits": order.credits_granted, "plan_type": _order_plan_type(order)}


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
        return {"status": "paid", "credits": order.credits_granted, "tier": order.tier, "plan_type": _order_plan_type(order)}

    if order.status == "failed":
        return {"status": "failed"}

    dodo_status = await dodo_svc.get_checkout_status(request.session_id)
    logger.info("[PAYMENT] Dodo checkout %s status from API: %s", request.session_id, dodo_status)

    if dodo_status["status"] in ("succeeded", "paid", "complete", "completed"):
        order.dodo_payment_id = dodo_status.get("payment_id", "")
        order.status = "paid"
        order.updated_at = datetime.utcnow()
        _finalize_credits(db, order)

        if order.coupon_id:
            db.query(Coupon).filter_by(id=order.coupon_id).update({"uses": Coupon.uses + 1})

        db.commit()
        logger.info("[PAYMENT] Dodo payment verified via API: checkout=%s, plan=%s",
                    request.session_id, order.plan_id)
        capture("payment_confirmed", str(order.user_id), {
            "plan_id": order.plan_id,
            "plan_type": _order_plan_type(order),
            "credits_granted": order.credits_granted,
            "provider": "dodo",
            "amount_cents": order.amount_cents,
            "currency": order.currency,
            "country": order.geo_country,
        })
        from services.stage_tracking import safe_mark_stage
        safe_mark_stage(db, str(order.user_id), "payment_made")
        return {"status": "paid", "credits": order.credits_granted, "tier": order.tier, "plan_type": _order_plan_type(order)}

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

        if order.status == "paid":
            logger.info("[DODO_WEBHOOK] Order already paid: %s", checkout_id)
            return {"status": "ok"}

        order.dodo_payment_id = payment_id
        order.status = "paid"
        order.updated_at = datetime.utcnow()

        _finalize_credits(db, order)

        if order.coupon_id:
            db.query(Coupon).filter_by(id=order.coupon_id).update({"uses": Coupon.uses + 1})

        db.commit()
        logger.info("[DODO_WEBHOOK] Payment succeeded: checkout=%s, plan=%s, user %s",
                    checkout_id, order.plan_id, order.user_id)

        from services.stage_tracking import safe_mark_stage
        safe_mark_stage(db, str(order.user_id), "payment_made")

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
                order.updated_at = datetime.utcnow()
                _finalize_credits(db, order)
                if order.coupon_id:
                    db.query(Coupon).filter_by(id=order.coupon_id).update({"uses": Coupon.uses + 1})
                db.commit()
                logger.info("[PAYMENT_WEBHOOK] Payment captured: %s, plan=%s", rz_order_id, order.plan_id)
                from services.stage_tracking import safe_mark_stage
                safe_mark_stage(db, str(order.user_id), "payment_made")

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
    """Add email credits to user's balance. Creates row if not exists."""
    credit = db.query(UserCredit).filter_by(user_id=user_id).first()
    if credit:
        credit.total_credits += amount
        credit.updated_at = datetime.utcnow()
    else:
        credit = UserCredit(user_id=user_id, total_credits=amount)
        db.add(credit)


def _set_plan_on_order(db: Session, outreach_order_id: int | None, plan) -> None:
    """Set plan_type, leads_target, and linkedin_credits_reserved on the linked OutreachOrder."""
    if not outreach_order_id:
        return
    oo = db.query(OutreachOrder).filter_by(id=outreach_order_id).first()
    if not oo:
        return
    oo.plan_type = plan.plan_type
    if plan.email_credits:
        oo.leads_target = plan.email_credits
    if plan.linkedin_credits:
        oo.linkedin_credits_reserved = plan.linkedin_credits


def _finalize_credits(db: Session, order: PaymentOrder) -> None:
    """Grant email credits and set LinkedIn credits after a confirmed payment."""
    # Resolve plan — prefer plan_id column, fall back to legacy tier for old orders
    email_credits = 0
    linkedin_credits = 0
    plan_type = "email"

    if order.plan_id:
        try:
            from core.pricing import get_plan as _gp
            plan = _gp(order.plan_id)
            email_credits = plan.email_credits
            linkedin_credits = plan.linkedin_credits
            plan_type = plan.plan_type
        except Exception:
            email_credits = order.tier
    else:
        email_credits = order.tier

    order.credits_granted = email_credits

    if email_credits:
        _grant_credits(db, order.user_id, email_credits)

    if linkedin_credits and order.outreach_order_id:
        oo = db.query(OutreachOrder).filter_by(id=order.outreach_order_id).first()
        if oo:
            oo.linkedin_credits_reserved = linkedin_credits
            oo.plan_type = plan_type

    # Safety net: a paid order must never stay frozen behind the payment step.
    # The frontend normally advances order.status, but if that call is missed the
    # user is stuck at an early stage and the app re-shows "pay" (support ticket #19,
    # Ayesha: paid + credited but order frozen at 'created'). Always promote an
    # early-stage order to campaign_setup on payment so they can build their campaign.
    if order.outreach_order_id:
        oo2 = db.query(OutreachOrder).filter_by(id=order.outreach_order_id).first()
        _FROZEN = ("created", "leads_generating", "leads_ready", "enriching", "enrichment_complete")
        if oo2 and oo2.status in _FROZEN:
            _prev = oo2.status
            oo2.status = "campaign_setup"
            log = list(oo2.action_log or [])
            log.append({
                "ts": datetime.utcnow().isoformat(),
                "msg": f"Auto-advanced {_prev} -> campaign_setup on payment (status was frozen behind payment)",
            })
            oo2.action_log = log
            oo2.updated_at = datetime.utcnow()
            logger.info("[PAYMENT] Advanced outreach_order %s (%s -> campaign_setup) on payment finalize", oo2.id, _prev)


def _order_plan_type(order: PaymentOrder) -> str:
    if order.plan_id:
        try:
            from core.pricing import get_plan as _gp
            return _gp(order.plan_id).plan_type
        except Exception:
            pass
    return "email"


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
