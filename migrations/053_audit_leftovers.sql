-- 053_audit_leftovers.sql
-- Post-payment audit: the remaining prescribed parts of P01, P14, P31, P44.
--
-- 1. P01: campaigns.outcome. A campaign that finishes having delivered less
--    than half its first touches is marked 'degraded' (the founders are told),
--    so a campaign that delivered 1 email of 503 is never just "completed".
-- 2. P44: outreach_orders.campaign_id no longer silently nulls when a campaign
--    row is deleted by hand (ON DELETE SET NULL left orders at campaign_running
--    pointing at nothing). NO ACTION refuses such a delete; account deletion is
--    unaffected because it deletes a user's orders before their campaigns.
-- 3. P31: enrichment_jobs. Jobs lived in a per-process dict, so a restart lost
--    the job and its reservation (51 credits for one paying user). Now any
--    replica can answer a status poll, and the reconciler releases what a dead
--    job still holds.
-- 4. P14: coupons.uses backfilled from real redemptions (it read 1 while one
--    100% code had been redeemed 22 times), and max_uses becomes mandatory with
--    a default, so no new coupon can be uncapped. Existing uncapped coupons of
--    50% or more are capped at their current uses (already blocked in
--    production by #100); smaller ones get 1000.
--
-- Apply BEFORE deploying the code that maps campaigns.outcome and enrichment_jobs.

BEGIN;

ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS outcome VARCHAR(20);

ALTER TABLE outreach_orders DROP CONSTRAINT IF EXISTS outreach_orders_campaign_id_fkey;
ALTER TABLE outreach_orders ADD CONSTRAINT outreach_orders_campaign_id_fkey
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE NO ACTION;

CREATE TABLE IF NOT EXISTS enrichment_jobs (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    status      VARCHAR(20) NOT NULL,
    reserved    INTEGER NOT NULL DEFAULT 0,
    released    INTEGER NOT NULL DEFAULT 0,
    total       INTEGER NOT NULL DEFAULT 0,
    enriched    INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    progress    TEXT,
    error       TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at  TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX IF NOT EXISTS ix_enrichment_jobs_status ON enrichment_jobs (status, updated_at);

UPDATE coupons c SET uses = sub.n
FROM (
    SELECT c2.id, count(p.id) AS n
    FROM coupons c2
    LEFT JOIN payment_orders p ON p.coupon_id = c2.id AND p.status IN ('paid', 'completed')
    GROUP BY c2.id
) sub
WHERE sub.id = c.id AND c.uses <> sub.n;

UPDATE coupons SET max_uses = uses WHERE max_uses IS NULL AND discount_value >= 50;
UPDATE coupons SET max_uses = GREATEST(uses, 1000) WHERE max_uses IS NULL;
ALTER TABLE coupons ALTER COLUMN max_uses SET DEFAULT 100;
ALTER TABLE coupons ALTER COLUMN max_uses SET NOT NULL;

COMMIT;
