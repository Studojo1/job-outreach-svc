"""One-time script: Create a single Dodo product with pay_what_you_want enabled,
and archive the old per-tier products.

Usage:
    DODO_PAYMENTS_API_KEY=sk_test_... python scripts/setup_dodo_product.py

Or if you have a .env file:
    source .env && python scripts/setup_dodo_product.py
"""

import os
import sys

from dodopayments import DodoPayments

api_key = os.environ.get("DODO_PAYMENTS_API_KEY")
if not api_key:
    print("ERROR: Set DODO_PAYMENTS_API_KEY env var first")
    print("  export DODO_PAYMENTS_API_KEY=sk_test_...")
    sys.exit(1)

test_mode = os.environ.get("DODO_TEST_MODE", "true").lower() == "true"
env = "test_mode" if test_mode else "live_mode"
print(f"Environment: {env}")

client = DodoPayments(bearer_token=api_key, environment=env)

# Old per-tier product IDs to archive
OLD_PRODUCTS = [
    "pdt_0NbFku1lELaEEGgVWQqK8",  # Outreach Starter — 200
    "pdt_0NbFku8V0GHYDKlHUPSrW",  # Outreach Growth — 350
    "pdt_0NbFkuB1MlEATwuf1b4Yl",  # Outreach Scale — 500
]

# Step 1: Create new unified product with pay_what_you_want
print("\nCreating unified product with pay_what_you_want...")
product = client.products.create(
    name="Outreach Email Credits",
    description="Email enrichment credits for OpportunityApply outreach campaigns. Amount determined at checkout.",
    tax_category="digital_products",
    price={
        "type": "one_time_price",
        "currency": "USD",
        "price": 100,          # $1 minimum
        "discount": 0,
        "purchasing_power_parity": False,
        "pay_what_you_want": True,
        "suggested_price": None,
        "tax_inclusive": None,
    },
)

print(f"  Created: {product.product_id} — {product.name}")

# Step 2: Archive old products
print("\nArchiving old per-tier products...")
for pid in OLD_PRODUCTS:
    try:
        # Dodo doesn't have archive via SDK, but we can update to mark them
        # Just skip if not found
        client.products.update(pid, name=f"[ARCHIVED] {pid}", description="Replaced by unified product")
        print(f"  Archived: {pid}")
    except Exception as e:
        print(f"  Skip {pid}: {e}")

print("\n" + "=" * 60)
print("SUCCESS!")
print("=" * 60)
print(f"\nNew product ID: {product.product_id}")
print(f"\nAdd to K8s staging secrets:")
print(f"  DODO_PRODUCT_OUTREACH={product.product_id}")
print(f"\nRemove these old env vars:")
print(f"  DODO_PRODUCT_OUTREACH_200")
print(f"  DODO_PRODUCT_OUTREACH_350")
print(f"  DODO_PRODUCT_OUTREACH_500")
