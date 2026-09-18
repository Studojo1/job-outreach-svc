-- 042_email_open_tracking.sql
-- Open-rate tracking via a 1x1 tracking pixel embedded in outreach emails.
--
-- Each sent email gets a random `tracking_token`. The email body carries a
-- pixel <img> pointing at GET /job-outreach/t/{token}.png. When the lead's
-- mail client loads the pixel, the endpoint stamps first_opened_at /
-- last_opened_at / open_count on the matching row.
--
-- NOTE: pixel-based opens are approximate (Apple Mail Privacy Protection and
-- Gmail's image proxy pre-fetch images), so we store counts + timestamps, not
-- just a boolean, and the endpoint filters obvious prefetches.
--
-- Additive + nullable => safe on the shared DB.

BEGIN;

-- Per-email random token embedded in the pixel URL (secrets.token_urlsafe(24)).
ALTER TABLE emails_sent ADD COLUMN IF NOT EXISTS tracking_token VARCHAR(64);

-- Open tracking. first_opened_at is set once; last_opened_at updates on every
-- pixel load; open_count increments each load.
ALTER TABLE emails_sent ADD COLUMN IF NOT EXISTS first_opened_at TIMESTAMP;
ALTER TABLE emails_sent ADD COLUMN IF NOT EXISTS last_opened_at TIMESTAMP;
ALTER TABLE emails_sent ADD COLUMN IF NOT EXISTS open_count INTEGER DEFAULT 0;

-- Unique lookup of a sent email by its pixel token (partial: only real tokens).
CREATE UNIQUE INDEX IF NOT EXISTS idx_emails_sent_tracking_token
    ON emails_sent (tracking_token)
    WHERE tracking_token IS NOT NULL;

-- Efficient count of opened emails per campaign for the open-rate metric.
CREATE INDEX IF NOT EXISTS idx_emails_sent_opened_campaign
    ON emails_sent (campaign_id)
    WHERE open_count > 0;

COMMIT;
