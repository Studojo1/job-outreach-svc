"""High-discount codes with no use cap must not work in production.

TREAT100 and SAVE99 (99%, unlimited) are internal testing tools: a leaked one
would give away plans on the live site, so production rejects them and staging
(test mode) does not. Capped high-discount codes are deliberate, bounded grants
and keep working: FREE100 (LinkedIn promo, 2 uses), PRASHIKA100 (one person),
OAUTH100 (capped for Google's app review).
"""
from decimal import Decimal

from core.pricing import INTERNAL_DISCOUNT_FLOOR, is_internal_only_coupon


def test_unlimited_internal_codes_are_blocked():
    assert is_internal_only_coupon("percent", 99.0, None)    # TREAT100, SAVE99
    assert is_internal_only_coupon("percent", 100.0, None)   # any future unlimited 100%


def test_capped_grants_keep_working():
    assert not is_internal_only_coupon("percent", 100.0, 2)   # FREE100
    assert not is_internal_only_coupon("percent", 100.0, 1)   # PRASHIKA100
    assert not is_internal_only_coupon("percent", 100.0, 11)  # OAUTH100 after capping


def test_every_ambassador_and_partner_code_stays_allowed():
    for pct in (10.0, 13.0, 15.0, 20.0):
        assert not is_internal_only_coupon("percent", pct, None)


def test_the_boundary():
    assert not is_internal_only_coupon("percent", 49.9, None)
    assert is_internal_only_coupon("percent", INTERNAL_DISCOUNT_FLOOR, None)
    assert is_internal_only_coupon("percent", 50.1, None)


def test_flat_discounts_are_never_treated_as_internal():
    """A flat value is paise, not a percentage - 5000 is Rs 50, not 5000%."""
    assert not is_internal_only_coupon("flat", 5000, None)
    assert not is_internal_only_coupon("flat", 100000, None)


def test_decimal_from_the_database_is_handled():
    assert is_internal_only_coupon("percent", Decimal("99.00"), None)
    assert not is_internal_only_coupon("percent", Decimal("10.00"), None)
    assert not is_internal_only_coupon("percent", Decimal("100.00"), 2)
