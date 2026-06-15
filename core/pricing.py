"""Pricing configuration — email, LinkedIn, and both-channel plans."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class PlanTier:
    plan_id: str           # e.g. "email_200", "linkedin_350", "both_500"
    plan_type: str         # "email" | "linkedin" | "both"
    label: str             # "Starter" | "Growth" | "Scale"
    email_credits: int     # 0 for linkedin-only plans
    linkedin_credits: int  # 0 for email-only plans
    price_usd: int         # cents (0 = India-only plan, not offered internationally)
    price_inr: int         # paise
    duration_days: int = 0 # 0 = unlimited; >0 = campaign auto-completes after N days


# Production plans — 10 tiers (email x4, linkedin x3, both x3)
PLANS: list[PlanTier] = [
    # ── Email-only ──────────────────────────────────────────────────────────
    # email_50: India-only entry plan. price_usd=0 means it won't surface for USD users.
    # INR anchors (for visual "save X%") live in routes_payment.py:
    #   ₹2500 → ₹1825 (27% off) · ₹3500 → ₹2325 (34% off) · ₹5000 → ₹3465 (31% off)
    PlanTier("email_50",     "email",    "Starter",  50, 0,      0,  49900, duration_days=8),
    PlanTier("email_200",    "email",    "Growth",  200, 0,   2000,  182500),
    PlanTier("email_350",    "email",    "Pro",     350, 0,   2700,  232500),
    PlanTier("email_500",    "email",    "Scale",   500, 0,   5000,  346500),
    # ── LinkedIn-only ───────────────────────────────────────────────────────
    PlanTier("linkedin_200", "linkedin", "Starter", 0, 200,   2000,  182500),
    PlanTier("linkedin_350", "linkedin", "Growth",  0, 350,   2700,  232500),
    PlanTier("linkedin_500", "linkedin", "Scale",   0, 500,   5000,  346500),
    # ── Both channels ───────────────────────────────────────────────────────
    PlanTier("both_200",     "both",     "Starter", 200, 200, 3500,  299900),
    PlanTier("both_350",     "both",     "Growth",  350, 350, 4500,  399900),
    PlanTier("both_500",     "both",     "Scale",   500, 500, 7000,  499900),
]

# Test-mode plans (~$1 / ₹90) — used when RAZORPAY_TEST_MODE=true
TEST_PLANS: list[PlanTier] = [
    PlanTier("email_50",     "email",    "Starter",  50, 0,   100,   9000, duration_days=8),
    PlanTier("email_200",    "email",    "Growth",  200, 0,   100,   9000),
    PlanTier("email_350",    "email",    "Pro",     350, 0,   100,   9000),
    PlanTier("email_500",    "email",    "Scale",   500, 0,   100,   9000),
    PlanTier("linkedin_200", "linkedin", "Starter", 0, 200,   100,   9000),
    PlanTier("linkedin_350", "linkedin", "Growth",  0, 350,   100,   9000),
    PlanTier("linkedin_500", "linkedin", "Scale",   0, 500,   100,   9000),
    PlanTier("both_200",     "both",     "Starter", 200, 200, 100,   9000),
    PlanTier("both_350",     "both",     "Growth",  350, 350, 100,   9000),
    PlanTier("both_500",     "both",     "Scale",   500, 500, 100,   9000),
]

# Convenience index
_PLANS_BY_ID: dict[str, PlanTier] = {p.plan_id: p for p in PLANS}
_TEST_PLANS_BY_ID: dict[str, PlanTier] = {p.plan_id: p for p in TEST_PLANS}


def get_plan(plan_id: str, test_mode: bool = False) -> PlanTier:
    source = _TEST_PLANS_BY_ID if test_mode else _PLANS_BY_ID
    plan = source.get(plan_id)
    if not plan:
        raise ValueError(f"Invalid plan_id: {plan_id}. Must be one of {list(source.keys())}")
    return plan


def get_plans(test_mode: bool = False) -> list[PlanTier]:
    return TEST_PLANS if test_mode else PLANS


# ── Backwards-compat shims (email-only tier int → plan) ─────────────────────

@dataclass(frozen=True)
class TierPricing:
    tier: int
    price_usd: int
    price_inr: int
    label: str


TIERS: dict[int, TierPricing] = {
    5:   TierPricing(tier=5,   price_usd=0,    price_inr=0,      label="Test"),
    50:  TierPricing(tier=50,  price_usd=0,    price_inr=49900,  label="Starter"),
    200: TierPricing(tier=200, price_usd=2000, price_inr=182500, label="Growth"),
    350: TierPricing(tier=350, price_usd=2700, price_inr=232500, label="Pro"),
    500: TierPricing(tier=500, price_usd=5000, price_inr=346500, label="Scale"),
}

TEST_TIERS: dict[int, TierPricing] = {
    5:   TierPricing(tier=5,   price_usd=0,   price_inr=0,    label="Test"),
    50:  TierPricing(tier=50,  price_usd=100, price_inr=9000, label="Starter"),
    200: TierPricing(tier=200, price_usd=100, price_inr=9000, label="Growth"),
    350: TierPricing(tier=350, price_usd=100, price_inr=9000, label="Pro"),
    500: TierPricing(tier=500, price_usd=100, price_inr=9000, label="Scale"),
}


def get_tier_pricing(tier: int, test_mode: bool = False) -> TierPricing:
    source = TEST_TIERS if test_mode else TIERS
    pricing = source.get(tier)
    if not pricing:
        raise ValueError(f"Invalid tier: {tier}. Must be one of {list(source.keys())}")
    return pricing


def get_dodo_product_id(settings) -> str:
    if not settings.DODO_PRODUCT_OUTREACH:
        raise ValueError("DODO_PRODUCT_OUTREACH not configured")
    return settings.DODO_PRODUCT_OUTREACH


def apply_coupon(amount_cents: int, discount_type: str, discount_value: float) -> int:
    """Apply coupon discount. Returns final amount in cents/paise (minimum 0)."""
    if discount_type == "percent":
        discount = int(amount_cents * discount_value / 100)
    else:  # flat
        discount = int(discount_value)
    return max(0, amount_cents - discount)
