"""Payment Routes — Razorpay + Dodo Payments with geo-based routing."""

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, text
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from database.session import get_db
from database.models import User, Coupon, PaymentOrder, UserCredit, OutreachOrder
from core.config import settings
from core.pricing import (
    get_plan, get_plans, get_tier_pricing, get_dodo_product_id, apply_coupon,
    is_internal_only_coupon, sellable_email_packs,
)
from core.geo import detect_country, get_client_ip, is_india
from api.dependencies import get_current_user
from core.analytics import capture
from services.payment_receipt import send_receipt
from core import meta_capi
import services.dodo_payments as dodo_svc
from services import webhook_health
from services import credits

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payment", tags=["Payment"])


def _run_async(fn, *args):
    """Call an async helper from a sync handler running on FastAPI's
    threadpool (audit P15: the payment handlers no longer run their
    synchronous DB work on the one event loop)."""
    import anyio.from_thread
    return anyio.from_thread.run(fn, *args)


# Google's OAuth reviewers use OAUTH100 (CLAUDE.md: must keep working) and may
# reuse one test account across review rounds, so it is exempt from the
# one-per-user rule. Its max_uses cap still bounds it.
PER_USER_EXEMPT_COUPONS = {"OAUTH100"}


def _already_redeemed(db: Session, coupon_id: int, user_id: str) -> bool:
    code = db.query(Coupon.code).filter(Coupon.id == coupon_id).scalar()
    if code and code.strip().upper() in PER_USER_EXEMPT_COUPONS:
        return False
    return db.query(PaymentOrder.id).filter(
        PaymentOrder.coupon_id == coupon_id,
        PaymentOrder.user_id == user_id,
        PaymentOrder.status.in_(REDEEMED_STATUSES),
    ).first() is not None


# A coupon's use count is the number of payments it actually paid for (audit
# PP-P14). It used to be a counter bumped by hand in six places, one per
# confirmation path, so any path that ran twice or forgot the bump left it
# wrong, and max_uses was checked against it. Every path now sets it from the
# payments themselves, never lowering it (an admin may have raised it on
# purpose to close a code), and the hourly reconcile repairs any drift.
REDEEMED_STATUSES = ("paid", "completed", "refunding", "refunded")
# A checkout started with a capped code holds one of its remaining uses for
# this long, so a burst of checkouts cannot all pass the cap before any of
# them is paid. The buyer's own earlier checkouts do not count against them,
# so retrying a checkout never locks a student out of their own code.
CHECKOUT_HOLDS_COUPON = timedelta(minutes=30)


def coupon_redemptions(db: Session, coupon_id: int) -> int:
    return db.query(func.count(PaymentOrder.id)).filter(
        PaymentOrder.coupon_id == coupon_id,
        PaymentOrder.status.in_(REDEEMED_STATUSES),
    ).scalar() or 0


def sync_coupon_uses(db: Session, coupon_id: int | None) -> None:
    """Set coupons.uses from the payments it paid for. Idempotent; does not commit."""
    if not coupon_id:
        return
    db.flush()
    n = coupon_redemptions(db, coupon_id)
    db.query(Coupon).filter(Coupon.id == coupon_id, Coupon.uses < n) \
        .update({"uses": n}, synchronize_session=False)


def coupon_exhausted(db: Session, coupon: Coupon, user_id: str, now: datetime | None = None) -> bool:
    """True when a capped coupon has no use left for this buyer: paid uses plus
    other buyers' checkouts still in flight."""
    if coupon.max_uses is None:
        return False
    now = now or datetime.utcnow()
    held = db.query(func.count(PaymentOrder.id)).filter(
        PaymentOrder.coupon_id == coupon.id,
        PaymentOrder.status == "created",
        PaymentOrder.user_id != user_id,
        PaymentOrder.created_at >= now - CHECKOUT_HOLDS_COUPON,
    ).scalar() or 0
    used = max(coupon.uses or 0, coupon_redemptions(db, coupon.id))
    return used + held >= coupon.max_uses


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
    # No struck-out "original" prices: Rs 2,500 / 3,500 / 5,000 were never
    # charged, so showing them is a false reference price (audit OP-N04).
    # Bring one back only when a real, dated previous price exists.

    tiers = []
    for p in result:
        if p["plan_type"] != "email":
            continue
        tier_num = p["email_credits"]
        tiers.append({
            "tier": tier_num,
            "label": p["label"],
            "amount_cents": p["amount_cents"],
            "currency": p["currency"],
            "display_price": p["display_price"],
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
    if coupon_exhausted(db, coupon, current_user.id, now):
        raise HTTPException(status_code=400, detail="Coupon usage limit reached")
    # High-discount internal codes work on staging only. Same 404 an unknown
    # code gets, so probing can't distinguish "blocked" from "does not exist".
    if not settings.RAZORPAY_TEST_MODE and is_internal_only_coupon(
        coupon.discount_type, coupon.discount_value, coupon.max_uses
    ):
        raise HTTPException(status_code=404, detail="Invalid coupon code")
    # Per-recipient founder coupons are bound to one buyer — reject if someone
    # else tries to redeem a leaked code.
    if coupon.user_id and str(coupon.user_id) != str(current_user.id):
        raise HTTPException(status_code=403, detail="This coupon is not valid for your account")

    # Support both plan_id (new) and tier (legacy)
    if request.plan_id:
        plan = get_plan(request.plan_id, settings.RAZORPAY_TEST_MODE)
        # Coupons apply to the LinkedIn weekly plan only — never the monthly plan.
        if plan.plan_type == "linkedin" and plan.plan_id != "linkedin_weekly":
            raise HTTPException(status_code=400, detail="Coupons apply to the weekly plan only.")
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
    # EX-07: the browser's _fbp/_fbc cookies (or an fbc built from the stored
    # fbclid), forwarded to the server-side Meta Purchase. Optional: ad
    # blockers and cookie refusals leave them absent, and that is fine.
    fbp: Optional[str] = None
    fbc: Optional[str] = None
    # HP-N13: the visitor's cookie choice ("granted" / "denied" / None when
    # they were never asked) and device time zone, so the server-side Meta
    # Purchase honours the same consent as the browser pixel.
    tracking_consent: Optional[str] = None
    time_zone: Optional[str] = None
    # UC-Q09: the candidate whose leads the pricing page showed. Optional; the
    # server falls back to the user's active candidate.
    candidate_id: Optional[int] = None


def _meta_signal(value: Optional[str]) -> Optional[str]:
    """A Meta browser id as sent by the client, or None if it is not one.

    Both cookies are "fb.<n>.<timestamp>.<value>"; anything else is not worth
    storing or forwarding.
    """
    v = (value or "").strip()
    return v[:255] if v.startswith("fb.") else None


def _meta_signals(body: CreateOrderRequest, req: Request, country: Optional[str] = None) -> dict:
    """PaymentOrder columns holding the buyer's match signals (EX-07).

    Captured at create-order because the Purchase is reported later, often
    from a webhook that has no browser behind it.

    Without consent (HP-N13) none of them is kept, and meta_fbp carries a
    mark that tells _report_purchase_to_meta not to report this order.
    """
    if not meta_capi.meta_allowed(body.tracking_consent, body.time_zone, country):
        return {"meta_fbp": meta_capi.NO_CONSENT_MARK, "meta_fbc": None,
                "client_ip": None, "client_user_agent": None}
    ip = get_client_ip(req)
    return {
        "meta_fbp": _meta_signal(body.fbp),
        "meta_fbc": _meta_signal(body.fbc),
        "client_ip": ip[:64] if ip and ip != "0.0.0.0" else None,  # noqa: S104 - string comparison, not a bind
        "client_user_agent": (req.headers.get("user-agent") or "")[:1000] or None,
    }


def _has_something_to_send(db: Session, user_id: str) -> bool:
    from database.models import Candidate, Lead
    has_leads = (
        db.query(Lead.id).join(Candidate, Candidate.id == Lead.candidate_id)
        .filter(Candidate.user_id == user_id).first() is not None
    )
    if has_leads:
        return True
    try:
        with db.begin_nested():
            return db.execute(
                text("SELECT 1 FROM extension_drafts WHERE user_id = :u LIMIT 1"), {"u": user_id}
            ).first() is not None
    except Exception:
        # Never block a payment on a failed check.
        logger.warning("[PAYMENT] extension_drafts check failed for %s; allowing", user_id)
        return True


def _strong_pool(db: Session, user_id: str, candidate_id: Optional[int]) -> Optional[int]:
    """Strong matches on the candidate being bought for, or None when the user
    has no candidate with leads (the pack cap then does not apply)."""
    from api.routes_candidate import _active_candidate_with_leads, count_strong_leads
    from database.models import Candidate
    try:
        with db.begin_nested():
            cid = None
            if candidate_id is not None and db.query(Candidate.id).filter(
                    Candidate.id == candidate_id, Candidate.user_id == user_id).first():
                cid = candidate_id
            if cid is None:
                cid = _active_candidate_with_leads(db, user_id, exclude_id=-1)
            if cid is None:
                return None
            total, strong = count_strong_leads(db, cid)
            return strong if total else None
    except Exception:
        # Never block a payment on a failed check.
        logger.warning("[PAYMENT] strong-pool check failed for %s; allowing every pack", user_id)
        return None


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

    # Retired plans stay resolvable so historical orders, payments and credits still
    # work, and anyone already holding their credits can spend them. They just cannot
    # be bought again.
    if getattr(plan, "retired", False):
        raise HTTPException(
            status_code=400,
            detail="That plan is no longer available. Please choose one of the current plans.",
        )

    # Nothing to send to, nothing to sell (B2C UC-Q24). Only when the user has
    # no leads on any candidate and has never drafted from the extension, whose
    # users buy credits for one-off sends without running discovery.
    if (plan.email_credits or 0) > 0 and not _has_something_to_send(db, current_user.id):
        raise HTTPException(
            status_code=409,
            detail="We have not found hiring managers for you yet, so there is nothing to buy. Run the search again first.",
        )

    # Do not sell a pack much bigger than the student's pool of strong matches
    # (B2C UC-Q09). The smallest pack always stays sellable.
    if plan.plan_type == "email" and plan.email_credits:
        strong = _strong_pool(db, current_user.id, body.candidate_id)
        allowed = sellable_email_packs(strong, settings.RAZORPAY_TEST_MODE)
        if plan.email_credits not in allowed:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"We found {strong} strong matches for you, so a {plan.email_credits}-contact pack "
                    f"would mostly go to weaker matches. Please choose the {max(allowed)}-contact pack."
                ),
            )

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
            if coupon.valid_from and coupon.valid_from > now:
                valid = False
            if coupon_exhausted(db, coupon, current_user.id, now):
                valid = False
            # High-discount internal codes are staging-only.
            if not settings.RAZORPAY_TEST_MODE and is_internal_only_coupon(
                coupon.discount_type, coupon.discount_value, coupon.max_uses
            ):
                valid = False
            # Per-recipient founder coupons are bound to one buyer.
            if coupon.user_id and str(coupon.user_id) != str(current_user.id):
                valid = False
            # One redemption per user (audit P14: one user redeemed a 100% code
            # seven times for 550 credits).
            if _already_redeemed(db, coupon.id, current_user.id):
                valid = False
            # Coupons apply to the LinkedIn weekly plan only — never the monthly plan.
            if plan.plan_type == "linkedin" and resolved_plan_id != "linkedin_weekly":
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
        # Fully discounted — grant credits directly.
        # Two simultaneous free redemptions could both pass the checks above
        # (audit P14). Lock the coupon row and re-check under the lock; nothing
        # after this point waits on the network, so the lock is short.
        if coupon_id:
            locked = db.query(Coupon).filter_by(id=coupon_id).with_for_update().first()
            if (locked is None or not locked.is_active
                    or coupon_exhausted(db, locked, current_user.id)
                    or _already_redeemed(db, coupon_id, current_user.id)):
                db.rollback()
                raise HTTPException(status_code=400, detail="This coupon has already been used.")
        idem_key = str(uuid.uuid4())
        from services.stage_tracking import safe_mark_stage, get_or_create_active_order, promote_paid_order
        try:
            outreach_order = get_or_create_active_order(db, str(current_user.id))
            outreach_order_id = outreach_order.id
        except Exception:
            outreach_order = None
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
        db.flush()  # order.id for the ledger row
        if plan.email_credits:
            _grant_credits(db, current_user.id, plan.email_credits,
                           reason=credits.GRANT_COUPON, payment_order_id=order.id)
        sync_coupon_uses(db, coupon_id)
        _set_plan_on_order(db, outreach_order_id, plan)
        # Same safety net as a paid order (_finalize_credits): this path never
        # goes through it, so a coupon user was left frozen at 'created'.
        promote_paid_order(outreach_order, "100% coupon")
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
            raise HTTPException(status_code=400, detail=str(e)) from e

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
            raise HTTPException(status_code=502, detail="Payment gateway error. Please try again.") from e

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
            **_meta_signals(body, req, country),
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
        raise HTTPException(status_code=502, detail="Payment gateway error. Please try again.") from e

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
        **_meta_signals(body, req, country),
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
def verify_payment(
    request: VerifyPaymentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Verify Razorpay payment signature and grant credits."""
    # Lock the order row for the rest of this transaction. Dodo and Razorpay each
    # confirm a payment through TWO independent paths, the browser callback and the
    # provider's webhook, and both call _finalize_credits, which does
    # total_credits += amount. The `status == "paid"` guard below is not enough on
    # its own: with two API replicas the two requests can read the row at the same
    # instant, both see "created", and both grant. One customer ended up with
    # exactly double the credits they paid for, which then let them start a second
    # campaign. FOR UPDATE makes the second path wait and then see "paid".
    order = db.query(PaymentOrder).filter_by(
        razorpay_order_id=request.razorpay_order_id,
        user_id=current_user.id,
    ).with_for_update().first()

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

    sync_coupon_uses(db, order.coupon_id)

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

    _run_async(_report_purchase_to_meta, db, order)

    send_receipt(db, order)  # PS-N10

    return {"status": "verified", "credits": order.credits_granted, "plan_type": _order_plan_type(order)}


# ── Verify Dodo Payment (frontend polls after redirect) ──────────────────────

class VerifyDodoRequest(BaseModel):
    session_id: str


@router.post("/verify-dodo")
def verify_dodo_payment(
    request: VerifyDodoRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Check if a Dodo payment has been confirmed. Actively checks Dodo API if still pending."""
    # Lock the order row for the rest of this transaction. Dodo and Razorpay each
    # confirm a payment through TWO independent paths, the browser callback and the
    # provider's webhook, and both call _finalize_credits, which does
    # total_credits += amount. The `status == "paid"` guard below is not enough on
    # its own: with two API replicas the two requests can read the row at the same
    # instant, both see "created", and both grant. One customer ended up with
    # exactly double the credits they paid for, which then let them start a second
    # campaign. FOR UPDATE makes the second path wait and then see "paid".
    #
    # The lock is taken only AFTER the call to Dodo (audit P15). Holding it across
    # that await, with synchronous psycopg2 on a one-worker event loop, meant a
    # second poll for the same order blocked the whole pod (including /health)
    # while the first waited on Dodo. Read, ask Dodo, then lock and re-check.
    def _load(lock: bool):
        q = db.query(PaymentOrder).filter_by(dodo_checkout_id=request.session_id, user_id=current_user.id)
        return q.with_for_update().first() if lock else q.first()

    order = _load(lock=False)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status == "paid":
        return {"status": "paid", "credits": order.credits_granted, "tier": order.tier, "plan_type": _order_plan_type(order)}
    if order.status == "failed":
        return {"status": "failed"}
    db.rollback()  # end the read transaction before the network call

    dodo_status = _run_async(dodo_svc.get_checkout_status, request.session_id)
    logger.info("[PAYMENT] Dodo checkout %s status from API: %s", request.session_id, dodo_status)

    order = _load(lock=True)
    if order.status == "paid":  # the webhook or another poll got there first
        db.rollback()
        return {"status": "paid", "credits": order.credits_granted, "tier": order.tier, "plan_type": _order_plan_type(order)}
    if order.status == "failed":
        db.rollback()
        return {"status": "failed"}

    if dodo_status["status"] in ("succeeded", "paid", "complete", "completed"):
        order.dodo_payment_id = dodo_status.get("payment_id", "")
        order.status = "paid"
        order.updated_at = datetime.utcnow()
        _finalize_credits(db, order)

        sync_coupon_uses(db, order.coupon_id)

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
        _run_async(_report_purchase_to_meta, db, order)
        send_receipt(db, order)  # PS-N10
        return {"status": "paid", "credits": order.credits_granted, "tier": order.tier, "plan_type": _order_plan_type(order)}

    if dodo_status["status"] in ("failed", "expired", "cancelled"):
        order.status = "failed"
        order.updated_at = datetime.utcnow()
        db.commit()
        return {"status": "failed"}

    db.rollback()  # release the lock; nothing to change yet
    return {"status": "pending"}


# ── Dodo Webhook (server-to-server) ──────────────────────────────────────────

@router.post("/webhook/dodo")
async def dodo_webhook(request: Request, db: Session = Depends(get_db)):
    """Dodo Payments webhook handler. Verifies Standard Webhooks signature."""
    body = await request.body()

    # Fail closed, like the Razorpay webhook: with no secret every request was
    # accepted unsigned, so anyone could mark their own Dodo order paid (it is
    # unset on staging today). A rejected real webhook is not lost; verify-dodo
    # and payment_reconciler confirm the payment from Dodo's API.
    if not settings.DODO_WEBHOOK_SECRET:
        logger.error("[DODO_WEBHOOK] DODO_WEBHOOK_SECRET is not set; rejecting webhook")
        raise HTTPException(status_code=503, detail="Webhook not configured")
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
        webhook_health.record("dodo", ok=False)
        raise HTTPException(status_code=400, detail="Invalid webhook signature") from e
    webhook_health.record("dodo", ok=True)

    # Signature checked on the raw body above; the DB work runs on the
    # threadpool so a row lock can never stall the event loop (audit P15).
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(_dodo_webhook_apply, body, db)


def _dodo_webhook_apply(body: bytes, db: Session):
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

        # Locked for the same reason as the browser path above.
        order = db.query(PaymentOrder).filter_by(dodo_checkout_id=checkout_id).with_for_update().first()
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

        sync_coupon_uses(db, order.coupon_id)

        db.commit()
        logger.info("[DODO_WEBHOOK] Payment succeeded: checkout=%s, plan=%s, user %s",
                    checkout_id, order.plan_id, order.user_id)

        capture("payment_confirmed", str(order.user_id), {
            "plan_id": order.plan_id,
            "plan_type": _order_plan_type(order),
            "credits_granted": order.credits_granted,
            "provider": "dodo",
            "amount_cents": order.amount_cents,
            "currency": order.currency,
            "country": order.geo_country,
            "trigger": "webhook",  # "source" is reserved for source='server' (ST-N09)
        })

        from services.stage_tracking import safe_mark_stage
        safe_mark_stage(db, str(order.user_id), "payment_made")

        _run_async(_report_purchase_to_meta, db, order)

        send_receipt(db, order)  # PS-N10

    elif event_type == "refund.succeeded":
        # A refund made in the Dodo dashboard, or the echo of one we made (PP-P05).
        _settle_provider_refund(db, "dodo", data.get("payment_id"), data.get("refund_id"),
                                data.get("amount"), data.get("currency"))

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


def _settle_provider_refund(db: Session, provider: str, payment_id, refund_id, amount, currency) -> None:
    """Write a provider-side refund back into the app (PP-P05). A refund we are
    making right now answers 409 so the provider retries once we have settled."""
    from services.refunds import RefundInFlight, apply_provider_refund
    try:
        outcome = apply_provider_refund(
            db, provider=provider, payment_id=str(payment_id or ""), refund_id=str(refund_id or ""),
            amount_cents=int(amount) if amount is not None else None, currency=currency,
        )
        logger.info("[REFUND_WEBHOOK] %s refund %s on payment %s: %s", provider, refund_id, payment_id, outcome)
    except RefundInFlight as e:
        raise HTTPException(status_code=409, detail="Refund in progress; retry") from e


# ── Razorpay Webhook (server-to-server) ──────────────────────────────────────

@router.post("/webhook")
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    """Razorpay webhook handler. Verifies signature from X-Razorpay-Signature header."""
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    # Fail closed: with no secret configured, every request was accepted
    # unsigned, so anyone could mark their own order paid. A rejected real
    # webhook is not lost; payment_reconciler polls Razorpay for it.
    if not settings.RAZORPAY_WEBHOOK_SECRET:
        logger.error("[PAYMENT_WEBHOOK] RAZORPAY_WEBHOOK_SECRET is not set; rejecting webhook")
        raise HTTPException(status_code=503, detail="Webhook not configured")

    expected = hmac.new(
        settings.RAZORPAY_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        logger.error("[PAYMENT_WEBHOOK] Signature mismatch")
        webhook_health.record("razorpay", ok=False)
        raise HTTPException(status_code=400, detail="Invalid signature")
    webhook_health.record("razorpay", ok=True)

    # Signature checked on the raw body above; the DB work runs on the
    # threadpool so a row lock can never stall the event loop (audit P15).
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(_razorpay_webhook_apply, body, db)


def _razorpay_webhook_apply(body: bytes, db: Session):
    payload = json.loads(body)
    event = payload.get("event", "")

    if event == "payment.captured":
        payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        rz_order_id = payment_entity.get("order_id")
        rz_payment_id = payment_entity.get("id")

        if rz_order_id:
            # Locked for the same reason as the browser path above.
            order = db.query(PaymentOrder).filter_by(razorpay_order_id=rz_order_id).with_for_update().first()
            if order and order.status != "paid":
                order.razorpay_payment_id = rz_payment_id
                order.status = "paid"
                order.updated_at = datetime.utcnow()
                _finalize_credits(db, order)
                sync_coupon_uses(db, order.coupon_id)
                db.commit()
                logger.info("[PAYMENT_WEBHOOK] Payment captured: %s, plan=%s", rz_order_id, order.plan_id)
                capture("payment_confirmed", str(order.user_id), {
                    "plan_id": order.plan_id,
                    "plan_type": _order_plan_type(order),
                    "credits_granted": order.credits_granted,
                    "provider": "razorpay",
                    "amount_cents": order.amount_cents,
                    "currency": order.currency,
                    "country": order.geo_country,
                    "trigger": "webhook",  # "source" is reserved for source='server' (ST-N09)
                })
                from services.stage_tracking import safe_mark_stage
                safe_mark_stage(db, str(order.user_id), "payment_made")
                _run_async(_report_purchase_to_meta, db, order)
                send_receipt(db, order)  # PS-N10

    elif event == "refund.processed":
        # A refund made in the Razorpay dashboard, or the echo of one we made (PP-P05).
        refund = payload.get("payload", {}).get("refund", {}).get("entity", {})
        _settle_provider_refund(db, "razorpay", refund.get("payment_id"), refund.get("id"),
                                refund.get("amount"), refund.get("currency"))

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
    """Return the user's credit balance, and what those credits are actually doing.

    Credits are reserved in full the moment a campaign is created, not spent per
    send. So a user whose campaign is midway through reports used == total and
    available == 0, and the UI told them "You have 0 credits" next to a pricing
    page. To them that reads as money disappearing, when in fact their emails are
    queued and going out.

    `available_credits` keeps its old meaning so nothing that depends on it
    changes. The extra fields say where the reserved credits actually went, so the
    UI can show "20 sent, 30 scheduled" instead of a bare zero.
    """
    from database.models import Campaign, Candidate, EmailSent

    credit = db.query(UserCredit).filter_by(user_id=current_user.id).first()
    if not credit:
        return {"total_credits": 0, "used_credits": 0, "available_credits": 0,
                "emails_delivered": 0, "emails_scheduled": 0, "reserved_credits": 0,
                "has_active_campaign": False, "campaign_status": None}

    counts = (
        db.query(EmailSent.status, func.count(EmailSent.id))
        .join(Campaign, Campaign.id == EmailSent.campaign_id)
        .join(Candidate, Candidate.id == Campaign.candidate_id)
        .filter(Candidate.user_id == current_user.id,
                EmailSent.is_test.isnot(True),
                EmailSent.followup_number == 0)
        .group_by(EmailSent.status)
        .all()
    )
    by = {st: n for st, n in counts}
    delivered = by.get("sent", 0)
    scheduled = by.get("pending_enrichment", 0) + by.get("queued", 0)
    live = {
        st for (st,) in db.query(Campaign.status)
        .join(Candidate, Candidate.id == Campaign.candidate_id)
        .filter(Candidate.user_id == current_user.id,
                Campaign.status.in_(["running", "paused"]))
        .distinct()
    }
    active = bool(live)
    return {
        "total_credits": credit.total_credits,
        "used_credits": credit.used_credits,
        "available_credits": credit.total_credits - credit.used_credits,
        # what the reserved credits are actually doing
        "emails_delivered": delivered,
        "emails_scheduled": scheduled,
        "reserved_credits": max(0, credit.used_credits - delivered),
        "has_active_campaign": active,
        # has_active_campaign is true for paused campaigns too, so the pricing
        # page said "campaign running" for a paused one (PP-P39).
        "campaign_status": "running" if "running" in live else ("paused" if live else None),
    }


def _grant_credits(db: Session, user_id: str, amount: int,
                   reason: str = credits.GRANT_PAYMENT, payment_order_id: int | None = None):
    """Add email credits to user's balance (ledgered). Creates row if not exists."""
    credits.grant(db, user_id, amount, reason, payment_order_id=payment_order_id)


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


async def _report_purchase_to_meta(db: Session, order: PaymentOrder) -> None:
    """Send the authoritative Purchase to Meta, once per order.

    MUST be called AFTER db.commit(). Reporting before the commit risks telling
    Meta about revenue that then fails to persist, which is the one thing worse
    than missing the event.

    event_id is the payment provider's own id, which is exactly what the browser
    pixel sends, so Meta merges the two copies instead of counting the sale twice.
    Every paid path routes through here: Razorpay verify, Dodo verify, both
    webhooks and the stranded-order reconciler (services/payment_reconciler.py),
    so a payment confirmed while the user's tab is closed is still reported.
    """
    if not meta_capi.is_configured():
        return
    if order.meta_fbp == meta_capi.NO_CONSENT_MARK:
        # An EU/UK buyer who did not accept tracking (HP-N13).
        logger.info("[META_CAPI] Order %s: no tracking consent; Purchase not sent", order.id)
        return
    try:
        event_id = order.razorpay_order_id or order.dodo_checkout_id
        if not event_id:
            logger.warning("[META_CAPI] Order %s has no provider id; skipping Purchase", order.id)
            return

        email = None
        try:
            user = db.query(User).filter_by(id=order.user_id).first()
            email = user.email if user else None
        except Exception:
            pass  # match quality suffers, the event still counts

        await meta_capi.send_purchase(
            event_id=str(event_id),
            # PaymentOrder stores minor units; Meta wants major.
            value=(order.amount_cents or 0) / 100.0,
            currency=order.currency or "INR",
            email=email,
            external_id=str(order.user_id) if order.user_id else None,
            # EX-07: captured at create-order; without them Meta could match
            # this Purchase on the hashed email alone.
            client_ip=order.client_ip,
            user_agent=order.client_user_agent,
            fbp=order.meta_fbp,
            fbc=order.meta_fbc,
        )
    except Exception as e:
        # A payment must never fail because an analytics call did.
        logger.warning("[META_CAPI] Purchase reporting failed for order %s: %s", order.id, e)


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

    # Link the payment to the user's active order before anything reads the
    # link. A payment with no order link used to skip the safety net below
    # entirely (audit P12: 30 payments, 24 paying users).
    from services.stage_tracking import get_or_create_active_order, promote_paid_order
    if not order.outreach_order_id:
        try:
            active = get_or_create_active_order(db, str(order.user_id))
            order.outreach_order_id = active.id
            logger.info("[PAYMENT] linked unlinked payment %s to outreach_order %s", order.id, active.id)
        except Exception:
            logger.exception("[PAYMENT] could not link payment %s to an outreach order", order.id)

    if email_credits:
        _grant_credits(db, order.user_id, email_credits, payment_order_id=order.id)

    if linkedin_credits and order.outreach_order_id:
        oo = db.query(OutreachOrder).filter_by(id=order.outreach_order_id).first()
        if oo:
            oo.linkedin_credits_reserved = linkedin_credits
            oo.plan_type = plan_type

    # Safety net: a paid order must never stay frozen behind the payment step.
    if order.outreach_order_id:
        oo2 = db.query(OutreachOrder).filter_by(id=order.outreach_order_id).first()
        promote_paid_order(oo2, "status was frozen behind payment")


def _order_plan_type(order: PaymentOrder) -> str:
    if order.plan_id:
        try:
            from core.pricing import get_plan as _gp
            return _gp(order.plan_id).plan_type
        except Exception:
            pass
    return "email"


def deduct_credits(db: Session, user_id: str, amount: int,
                   reason: str = credits.RESERVE_ENRICHMENT) -> bool:
    """Deduct credits from user's balance. Returns False if insufficient.

    Locks the wallet row (credits.reserve), so two concurrent callers can no
    longer both pass the balance check (audit P31).
    """
    if amount <= 0:
        return True
    return credits.reserve(db, user_id, amount, reason) is not None


def refund_credits(db: Session, user_id: str, amount: int,
                   reason: str = credits.RELEASE_ENRICHMENT_UNUSED, campaign=None) -> int:
    """Give reserved credits back (e.g. enrichment failed). Returns the amount released."""
    return credits.release(db, user_id, amount, reason, campaign=campaign)
