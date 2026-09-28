-- 049_credit_ledger_and_campaign_lifecycle.sql
-- Post-payment audit, wave 1: the foundation the credit and lifecycle fixes
-- build on.
--
-- 1. credit_ledger. user_credits is a bare total/used pair with no history, so
--    nobody could say where a user's credits went (P11, P29): 2,200 credits sit
--    in 8 wallets with no payment behind them and it cannot be explained. From
--    now on every change to user_credits goes through services/credits.py,
--    which writes one row here in the same transaction. The rows are the
--    audit trail; user_credits stays the fast balance.
--
--    Each row carries the change to total_credits (delta_total, grants and
--    refunds of money) and to used_credits (delta_used, reserve > 0 and
--    release < 0), so for every user:
--      sum(delta_total) = user_credits.total_credits
--      sum(delta_used)  = user_credits.used_credits
--    The opening_balance rows below make that true from day one.
--
-- 2. campaigns.credits_reserved / credits_released. How much this campaign
--    took from the wallet and how much it has given back. Releases were
--    previously inferred from user_credits, which spans every campaign the
--    user ever ran (P30).
--
-- 3. campaigns.outreach_order_id. The order -> campaign link is currently one
--    pointer on outreach_orders that a second campaign overwrites (P03). The
--    reverse link lets an order own many campaigns. Backfilled from the
--    existing pointer; nothing reads it until the wave that fixes P03.
--
-- 4. campaigns.pause_reason / paused_by. Nobody could answer "why did my
--    campaign stop" (P40). paused_by is 'user', 'system' or an admin user id.
--
-- 5. emails_sent.status_changed_at. The auth-failure alert filtered on
--    created_at (launch time), so it read 0 for every dead mailbox (P17).
--
-- ORDER MATTERS. Apply this BEFORE deploying the code that maps these
-- columns. SQLAlchemy selects every mapped column, so a pod running the new
-- code against a database without them fails every campaigns / emails_sent
-- read. Every ALTER here is ADD COLUMN IF NOT EXISTS with a constant or no
-- default, which is metadata-only and instant on Postgres 11+.

BEGIN;

CREATE TABLE IF NOT EXISTS credit_ledger (
    id               BIGSERIAL PRIMARY KEY,
    user_id          TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    delta_total      INTEGER NOT NULL DEFAULT 0,
    delta_used       INTEGER NOT NULL DEFAULT 0,
    reason           VARCHAR(40) NOT NULL,
    campaign_id      INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    payment_order_id INTEGER REFERENCES payment_orders(id) ON DELETE SET NULL,
    actor            TEXT,
    note             TEXT,
    created_at       TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    CONSTRAINT credit_ledger_nonzero CHECK (delta_total <> 0 OR delta_used <> 0)
);
CREATE INDEX IF NOT EXISTS ix_credit_ledger_user ON credit_ledger (user_id, created_at);
CREATE INDEX IF NOT EXISTS ix_credit_ledger_campaign ON credit_ledger (campaign_id) WHERE campaign_id IS NOT NULL;

-- Opening balance: one row per existing wallet, so the ledger sums match
-- user_credits from the moment it exists. Idempotent: skipped for users who
-- already have one.
INSERT INTO credit_ledger (user_id, delta_total, delta_used, reason, note)
SELECT uc.user_id, uc.total_credits, uc.used_credits, 'opening_balance',
       'user_credits as of migration 049; history before this is not recorded'
FROM user_credits uc
WHERE (uc.total_credits <> 0 OR uc.used_credits <> 0)
  AND NOT EXISTS (
      SELECT 1 FROM credit_ledger cl
      WHERE cl.user_id = uc.user_id AND cl.reason = 'opening_balance'
  );

ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS credits_reserved INTEGER;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS credits_released INTEGER NOT NULL DEFAULT 0;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS outreach_order_id INTEGER
    REFERENCES outreach_orders(id) ON DELETE SET NULL;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS pause_reason TEXT;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS paused_by TEXT;
CREATE INDEX IF NOT EXISTS ix_campaigns_outreach_order ON campaigns (outreach_order_id);

-- Backfill the reverse link from the only link that exists today. Orphaned
-- campaigns (P03) stay NULL here; the wave that fixes P03 re-links them.
-- Two orders can point at one campaign; the most recent order wins.
UPDATE campaigns c
SET outreach_order_id = o.id
FROM (
    SELECT DISTINCT ON (campaign_id) campaign_id, id
    FROM outreach_orders
    WHERE campaign_id IS NOT NULL
    ORDER BY campaign_id, id DESC
) o
WHERE o.campaign_id = c.id AND c.outreach_order_id IS NULL;

ALTER TABLE emails_sent ADD COLUMN IF NOT EXISTS status_changed_at TIMESTAMP;

-- 6. launch_nudges. services/launch_nudge.py emails users who paid and never
--    launched. One row per email sent; it is also the dedupe, so a restart or
--    a second replica cannot double-send. Kept out of system_events because
--    the frontend shows a user their own system_events rows.
CREATE TABLE IF NOT EXISTS launch_nudges (
    id          BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    n           INTEGER NOT NULL,
    state       VARCHAR(30) NOT NULL,
    action_url  TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX IF NOT EXISTS ix_launch_nudges_user ON launch_nudges (user_id, created_at);

COMMIT;
