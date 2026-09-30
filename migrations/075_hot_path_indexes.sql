-- 075_hot_path_indexes.sql
-- Audit AR-D01 / AR-D02 / AR-D03 (areas audit, 30 Sep 2026).
--
-- pg_stat_user_tables on prod, 30 Sep:
--   emails_sent       55M sequential scans, 1.29 trillion rows read; about 80
--                     per minute steady, from the 30-second campaign cycle,
--                     which filters every campaign's rows by campaign_id and
--                     status and the send queue by status and scheduled_at.
--                     Only partial indexes existed on campaign_id, so none of
--                     those queries could use one.
--   company_profiles  4.1M sequential scans of the 94k-row table (170 billion
--                     rows read): the name fallback looked companies up with
--                     name ILIKE :name, which no btree index serves. The code
--                     now compares lower(name) = :name, served by the new
--                     expression index.
--   emails_sent.lead_id is a foreign key to leads with no index, so every lead
--   delete scanned emails_sent.
--
-- Three indexes are exact duplicates of a unique constraint's own index (the
-- ORM declared both unique=True and index=True). They cost a write on every
-- insert and, for lead_scores (4.2M rows), 90 MB. The unique index stays.
--
-- Every statement is CONCURRENTLY, so no table is locked against writes, and
-- IF [NOT] EXISTS, so the file can be re-run. CONCURRENTLY cannot run inside a
-- transaction: apply with plain psql (autocommit), never with -1 or BEGIN.
--   psql "$DATABASE_URL" -f migrations/075_hot_path_indexes.sql
-- If a CREATE is interrupted it leaves an INVALID index; drop it and re-run.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_emails_sent_campaign_status
    ON emails_sent (campaign_id, status);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_emails_sent_status_scheduled
    ON emails_sent (status, scheduled_at);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_emails_sent_lead_id
    ON emails_sent (lead_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_company_profiles_lower_name
    ON company_profiles (lower(name));

DROP INDEX CONCURRENTLY IF EXISTS idx_company_profiles_domain;        -- = company_profiles_domain_key
DROP INDEX CONCURRENTLY IF EXISTS lead_scores_lead_id_idx;            -- = uq_lead_scores_lead
DROP INDEX CONCURRENTLY IF EXISTS idx_payment_orders_razorpay_order_id; -- = payment_orders_razorpay_order_id_key
