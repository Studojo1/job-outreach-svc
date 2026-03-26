"""Pricing configuration for enrichment tiers."""

from dataclasses import dataclass

@dataclass(frozen=True)
class TierPricing:
    tier: int          # number of leads/credits
    price_usd: int     # price in USD cents
    price_inr: int     # price in INR paise
    label: str


TIERS: dict[int, TierPricing] = {
    5: TierPricing(tier=5, price_usd=0, price_inr=0, label="Test"),
    200: TierPricing(tier=200, price_usd=2000, price_inr=170000, label="Starter"),       # $20 / ₹1700
    350: TierPricing(tier=350, price_usd=2700, price_inr=229500, label="Growth"),         # $27 / ₹2295
    500: TierPricing(tier=500, price_usd=4000, price_inr=340000, label="Scale"),          # $40 / ₹3400
}

# Test-mode pricing (~$1 / ₹90) — used when RAZORPAY_TEST_MODE=true
TEST_TIERS: dict[int, TierPricing] = {
    5: TierPricing(tier=5, price_usd=0, price_inr=0, label="Test"),
    200: TierPricing(tier=200, price_usd=100, price_inr=9000, label="Starter"),           # $1 / ₹90
    350: TierPricing(tier=350, price_usd=100, price_inr=9000, label="Growth"),            # $1 / ₹90
    500: TierPricing(tier=500, price_usd=100, price_inr=9000, label="Scale"),             # $1 / ₹90
}


def get_tier_pricing(tier: int, test_mode: bool = False) -> TierPricing:
    source = TEST_TIERS if test_mode else TIERS
    pricing = source.get(tier)
    if not pricing:
        raise ValueError(f"Invalid tier: {tier}. Must be one of {list(source.keys())}")
    return pricing


def get_dodo_product_id(settings) -> str:
    """Return the single Dodo product ID (pay_what_you_want, amount set at checkout)."""
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
