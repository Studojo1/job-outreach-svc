-- 056_payment_order_meta_signals.sql
-- EX-07: the server-side Meta Purchase sent only a hashed email and user id,
-- so a sale whose browser pixel was blocked (ad blockers, iOS) or whose tab
-- closed before the success screen matched on email alone. These columns hold
-- the buyer's _fbp/_fbc cookies, IP and user agent from the create-order
-- request, so the Purchase reported later (verify or webhook) can carry them.
--
-- All nullable, no backfill. Apply BEFORE deploying the code that maps them:
-- the ORM selects every mapped column, so without these every payment_orders
-- query fails.

BEGIN;

ALTER TABLE payment_orders ADD COLUMN IF NOT EXISTS meta_fbp TEXT;
ALTER TABLE payment_orders ADD COLUMN IF NOT EXISTS meta_fbc TEXT;
ALTER TABLE payment_orders ADD COLUMN IF NOT EXISTS client_ip VARCHAR(64);
ALTER TABLE payment_orders ADD COLUMN IF NOT EXISTS client_user_agent TEXT;

COMMIT;
