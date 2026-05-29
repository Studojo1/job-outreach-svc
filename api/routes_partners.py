"""Partner API purchase endpoints (B2B bulk orders, no Studojo auth required).

This is for organisations like placement academies who buy API access in bulk.
Separate from the per-user OutreachOrder + credit flow because partners do not
consume credits in the platform — they call the API directly.
"""

import logging
import hmac
import hashlib
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr

from core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/partners", tags=["Partners"])


# ── Pricing ───────────────────────────────────────────────────────────────────
# D2C per-lead rate × (200 leads/candidate) = D2C bundle price.
# Bulk pricing (with code "preplaced sahil") was negotiated 25/05/26.

D2C_PRICE_PER_LEAD_INR = 850  # paise — Rs 8.50/lead
LEADS_PER_CANDIDATE = 200

BULK_DISCOUNT_CODE = "preplaced sahil"

# plan_key → (candidates, d2c_rate_paise, bulk_rate_paise, label)
PARTNER_PLANS = {
    "partner_50": {
        "candidates": 50,
        "leads": 50 * LEADS_PER_CANDIDATE,
        "d2c_rate_paise": D2C_PRICE_PER_LEAD_INR,
        "bulk_rate_paise": 680,  # Rs 6.80/lead
        "label": "Pilot",
    },
    "partner_200": {
        "candidates": 200,
        "leads": 200 * LEADS_PER_CANDIDATE,
        "d2c_rate_paise": D2C_PRICE_PER_LEAD_INR,
        "bulk_rate_paise": 600,  # Rs 6.00/lead
        "label": "Growth",
    },
    # 1000+ is contact-team only — no fixed price (Rs 4-5/lead range)
}


def _calc_amount_paise(plan_key: str, discount_applied: bool) -> int:
    p = PARTNER_PLANS[plan_key]
    rate = p["bulk_rate_paise"] if discount_applied else p["d2c_rate_paise"]
    # rate is paise per lead; total = rate × number of leads (units already match)
    return rate * p["leads"]


@router.get("/pricing")
async def get_partner_pricing(code: Optional[str] = None):
    """Return pricing for the public partners page.

    Without a code → D2C per-lead pricing for each tier.
    With code "preplaced sahil" → discounted bulk pricing.
    """
    discount_applied = (code or "").strip().lower() == BULK_DISCOUNT_CODE

    tiers = []
    for plan_key, p in PARTNER_PLANS.items():
        amount = _calc_amount_paise(plan_key, discount_applied)
        d2c_amount = p["d2c_rate_paise"] * p["leads"]
        tiers.append({
            "plan_key": plan_key,
            "label": p["label"],
            "candidates": p["candidates"],
            "leads": p["leads"],
            "rate_per_lead_inr": (p["bulk_rate_paise"] if discount_applied else p["d2c_rate_paise"]) / 100,
            "total_inr": amount / 100,
            "d2c_total_inr": d2c_amount / 100,
            "savings_inr": (d2c_amount - amount) / 100 if discount_applied else 0,
            "discount_applied": discount_applied,
        })

    return {
        "tiers": tiers,
        "discount_applied": discount_applied,
        "enterprise": {
            # 1000+ is contact-only; no price
            "min_candidates": 1000,
            "rate_range_inr": "4-5",
            "cta": "Contact team",
        },
    }


# ── Create Razorpay order ─────────────────────────────────────────────────────


class CreatePartnerOrderRequest(BaseModel):
    plan_key: str
    discount_code: Optional[str] = None
    buyer_email: EmailStr
    buyer_name: str
    organisation: str


@router.post("/create-order")
async def create_partner_order(body: CreatePartnerOrderRequest):
    if body.plan_key not in PARTNER_PLANS:
        raise HTTPException(status_code=400, detail="Invalid plan")

    discount_applied = (body.discount_code or "").strip().lower() == BULK_DISCOUNT_CODE
    amount_paise = _calc_amount_paise(body.plan_key, discount_applied)
    p = PARTNER_PLANS[body.plan_key]

    import razorpay
    client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
    rz_order = client.order.create({
        "amount": amount_paise,
        "currency": "INR",
        "notes": {
            "type": "partner",
            "plan_key": body.plan_key,
            "candidates": p["candidates"],
            "leads": p["leads"],
            "discount_applied": "true" if discount_applied else "false",
            "buyer_email": body.buyer_email,
            "buyer_name": body.buyer_name,
            "organisation": body.organisation,
        },
    })

    logger.info(
        "[PARTNER] Razorpay order %s — plan=%s amount=%d INR org=%s buyer=%s discount=%s",
        rz_order["id"], body.plan_key, amount_paise, body.organisation, body.buyer_email, discount_applied,
    )

    return {
        "order_id": rz_order["id"],
        "amount": amount_paise,
        "currency": "INR",
        "key_id": settings.RAZORPAY_KEY_ID,
        "plan_key": body.plan_key,
        "candidates": p["candidates"],
        "leads": p["leads"],
        "discount_applied": discount_applied,
    }


# ── Verify Razorpay payment ───────────────────────────────────────────────────


class VerifyPartnerPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


@router.post("/verify")
async def verify_partner_payment(body: VerifyPartnerPaymentRequest):
    # HMAC-SHA256(secret, order_id|payment_id) must equal signature
    msg = f"{body.razorpay_order_id}|{body.razorpay_payment_id}".encode()
    expected = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode(), msg, hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, body.razorpay_signature):
        logger.warning("[PARTNER] Signature mismatch for order %s", body.razorpay_order_id)
        raise HTTPException(status_code=400, detail="Invalid payment signature")

    # Fetch the order to get the buyer/notes back
    import razorpay
    client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
    try:
        order = client.order.fetch(body.razorpay_order_id)
        notes = order.get("notes", {}) or {}
    except Exception as e:
        logger.error("[PARTNER] Could not fetch order %s: %s", body.razorpay_order_id, e)
        notes = {}

    logger.info(
        "[PARTNER] Payment verified — order=%s payment=%s org=%s buyer=%s plan=%s",
        body.razorpay_order_id, body.razorpay_payment_id,
        notes.get("organisation"), notes.get("buyer_email"), notes.get("plan_key"),
    )

    return {
        "status": "verified",
        "order_id": body.razorpay_order_id,
        "payment_id": body.razorpay_payment_id,
        "plan_key": notes.get("plan_key"),
        "candidates": notes.get("candidates"),
        "leads": notes.get("leads"),
        "buyer_email": notes.get("buyer_email"),
        "organisation": notes.get("organisation"),
    }
