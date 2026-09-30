-- 070_payment_refunds_and_order_status.sql
-- B2C audit 30 Sep 2026, payments stream.
--
-- 1. PP-P05: payment_refunds, one row per refund a provider made on one of
--    our payments, whoever started it (the admin Refund button, the §3.3
--    campaign refund, or someone clicking Refund in the Razorpay or Dodo
--    dashboard, reported back by the provider's refund webhook). The unique
--    provider_refund_id makes a webhook for a refund we already settled a
--    no-op, so money and credits are settled exactly once.
-- 2. OP-N10: orders whose leads were generated but whose status never left
--    'created' / 'profile_complete' (2,737 on 29 Sep) move to 'leads_ready'.
--    Unpaid orders only, as prescribed. The hourly reconcile sweep keeps the
--    invariant from then on (services/reconcile.advance_orders_with_leads).
--
-- Apply BEFORE deploying the code that maps payment_refunds: the service
-- refuses to start when a mapped table or column is missing (CF-N05).

BEGIN;

CREATE TABLE IF NOT EXISTS payment_refunds (
    id                  SERIAL PRIMARY KEY,
    payment_order_id    INTEGER NOT NULL REFERENCES payment_orders(id) ON DELETE CASCADE,
    provider            VARCHAR(20) NOT NULL,
    provider_refund_id  TEXT NOT NULL UNIQUE,
    amount_cents        INTEGER NOT NULL,
    currency            VARCHAR(10),
    source              VARCHAR(20) NOT NULL,   -- 'admin' | 'policy_3_3' | 'webhook'
    actor               TEXT,
    reason              TEXT,
    credits_revoked     INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX IF NOT EXISTS ix_payment_refunds_order ON payment_refunds (payment_order_id);

UPDATE outreach_orders
SET status = 'leads_ready',
    updated_at = now() AT TIME ZONE 'utc'
WHERE status IN ('created', 'profile_complete')
  AND leads_generated_at IS NOT NULL
  AND payment_made_at IS NULL;

COMMIT;
