-- 020_jit_enrichment.sql
-- Just-In-Time enrichment: enrich leads hours before email send, not all at once.

BEGIN;

-- 1. leads: track enrichment retry failures
ALTER TABLE leads ADD COLUMN IF NOT EXISTS enrichment_fail_count INTEGER DEFAULT 0;

-- 2. emails_sent: support placeholder rows (no content/email yet)
--    to_email, subject, body are already nullable in the schema (no NOT NULL constraint),
--    but add enrichment_status to track JIT pipeline state.
ALTER TABLE emails_sent ADD COLUMN IF NOT EXISTS enrichment_status VARCHAR(20) DEFAULT 'pending';

-- Backfill: existing emails that already have content are fully enriched
UPDATE emails_sent SET enrichment_status = 'enriched' WHERE to_email IS NOT NULL;

-- 3. campaigns: persist style choices for JIT email generation
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS selected_styles JSONB;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS generation_mode VARCHAR(20) DEFAULT 'ai';

-- 4. outreach_orders: per-lead credit tracking
ALTER TABLE outreach_orders ADD COLUMN IF NOT EXISTS credits_reserved INTEGER DEFAULT 0;
ALTER TABLE outreach_orders ADD COLUMN IF NOT EXISTS credits_used INTEGER DEFAULT 0;
ALTER TABLE outreach_orders ADD COLUMN IF NOT EXISTS credits_refunded INTEGER DEFAULT 0;

-- 5. Index for the worker's JIT lookahead query
CREATE INDEX IF NOT EXISTS idx_emails_sent_jit
ON emails_sent (enrichment_status, scheduled_at)
WHERE enrichment_status = 'pending';

COMMIT;
