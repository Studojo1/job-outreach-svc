"""High-discount codes must not work in production.

TREAT100, SAVE99, FREE100, OAUTH100 and PRASHIKA100 hand out free or near-free
plans. They are testing tools, but they live in the same table as the campus
ambassador codes and staging shares production's database, so a leaked code
would work on the live site. Production rejects them; staging (test mode) does not.
"""
from core.pricing import INTERNAL_DISCOUNT_FLOOR, is_internal_only_coupon


def test_the_real_internal_codes_are_blocked():
    """Values taken from the live coupons table."""
    assert is_internal_only_coupon("percent", 100.0)  # FREE100, OAUTH100, PRASHIKA100
    assert is_internal_only_coupon("percent", 99.0)   # TREAT100, SAVE99


def test_every_ambassador_code_stays_allowed():
    """All nine campus ambassador codes are 10%."""
    for _ in range(9):
        assert not is_internal_only_coupon("percent", 10.0)


def test_the_other_live_distributor_codes_stay_allowed():
    """GOAT10/PEAR10 10%, FOLLOW13 13%, VANSH15 15%, aryansh 20%."""
    for pct in (10.0, 13.0, 15.0, 20.0):
        assert not is_internal_only_coupon("percent", pct)


def test_the_boundary():
    assert not is_internal_only_coupon("percent", 49.9)
    assert is_internal_only_coupon("percent", INTERNAL_DISCOUNT_FLOOR)
    assert is_internal_only_coupon("percent", 50.1)


def test_flat_discounts_are_never_treated_as_internal():
    """A flat value is paise, not a percentage - 5000 is ₹50, not 5000%."""
    assert not is_internal_only_coupon("flat", 5000)
    assert not is_internal_only_coupon("flat", 100000)


def test_decimal_from_the_database_is_handled():
    """discount_value comes back as Decimal, not float."""
    from decimal import Decimal
    assert is_internal_only_coupon("percent", Decimal("100.00"))
    assert is_internal_only_coupon("percent", Decimal("99.00"))
    assert not is_internal_only_coupon("percent", Decimal("10.00"))
