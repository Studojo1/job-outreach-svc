-- Migration 018: Add Dodo Payments support for geo-based payment routing
-- Adds provider tracking, Dodo-specific fields, and geo country to payment_orders

ALTER TABLE payment_orders
  ADD COLUMN IF NOT EXISTS provider VARCHAR(20) NOT NULL DEFAULT 'razorpay',
  ADD COLUMN IF NOT EXISTS dodo_checkout_id VARCHAR(255),
  ADD COLUMN IF NOT EXISTS dodo_payment_id VARCHAR(255),
  ADD COLUMN IF NOT EXISTS geo_country VARCHAR(5);

-- Index for looking up orders by Dodo checkout session ID (webhook + verify-dodo)
CREATE INDEX IF NOT EXISTS idx_payment_orders_dodo_checkout
  ON payment_orders(dodo_checkout_id) WHERE dodo_checkout_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_payment_orders_provider
  ON payment_orders(provider);