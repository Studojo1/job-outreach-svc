"""apply_coupon must never leave a fractional price at checkout.

A 10% coupon on the ₹499 plan was charging ₹449.10 — the discount was correct
but the price shown had paise in it. Amounts are stored in paise, so the result
has to land on a whole rupee.
"""
import pytest

from core.pricing import apply_coupon


@pytest.mark.parametrize(
    "price_paise,expected_paise,label",
    [
        (49900, 44900, "email_50 ₹499 -> ₹449"),
        (182500, 164200, "email_200 ₹1825 -> ₹1642"),
        (232500, 209200, "email_350 ₹2325 -> ₹2092"),
        (346500, 311800, "email_500 ₹3465 -> ₹3118"),
        (50000, 45000, "linkedin_weekly ₹500 -> ₹450"),
        (180000, 162000, "linkedin_monthly ₹1800 -> ₹1620"),
        (299900, 269900, "both_200 ₹2999 -> ₹2699"),
        (399900, 359900, "both_350 ₹3999 -> ₹3599"),
        (499900, 449900, "both_500 ₹4999 -> ₹4499"),
    ],
)
def test_ten_percent_lands_on_a_whole_rupee(price_paise, expected_paise, label):
    assert apply_coupon(price_paise, "percent", 10.0) == expected_paise, label


def test_no_discount_combination_leaves_paise():
    prices = (49900, 182500, 232500, 346500, 50000, 180000, 299900, 399900, 499900)
    for price in prices:
        for pct in (5.0, 10.0, 13.0, 15.0, 20.0, 27.0, 99.0):
            got = apply_coupon(price, "percent", pct)
            assert got % 100 == 0, f"{price} at {pct}% left paise: {got}"


def test_customer_is_never_charged_more_than_the_exact_discount():
    """Flooring must fall in the customer's favour."""
    for price in (49900, 182500, 232500, 346500, 299900):
        for pct in (10.0, 15.0, 20.0):
            exact = price - (price * pct / 100)
            assert apply_coupon(price, "percent", pct) <= exact


def test_hundred_percent_codes_still_grant_free():
    """FREE100 and OAUTH100 must stay at zero — create-order grants credits there."""
    assert apply_coupon(49900, "percent", 100.0) == 0
    assert apply_coupon(346500, "percent", 100.0) == 0
    assert apply_coupon(499900, "percent", 100.0) == 0


def test_ninety_nine_percent_codes_still_charge_something():
    """TREAT100 (99%) must not round down to free."""
    assert apply_coupon(182500, "percent", 99.0) == 1800   # ₹18
    assert apply_coupon(49900, "percent", 99.0) == 400     # ₹4


def test_flat_discounts_also_floor():
    assert apply_coupon(49900, "flat", 5000) == 44900      # ₹499 − ₹50
    assert apply_coupon(49900, "flat", 4990) == 44900      # ₹449.10 → ₹449


def test_discount_larger_than_price_clamps_to_zero():
    assert apply_coupon(49900, "flat", 99999) == 0
    assert apply_coupon(49900, "percent", 150.0) == 0


def test_zero_discount_leaves_the_price_untouched():
    assert apply_coupon(49900, "percent", 0.0) == 49900
