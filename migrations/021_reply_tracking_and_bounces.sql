-- 021_reply_tracking_and_bounces.sql
-- Reply tracking, bounce detection, test email flag, and Gmail polling state

BEGIN;

-- Store Gmail threadId from send response (needed to match replies to sent emails)
ALTER TABLE emails_sent ADD COLUMN IF NOT EXISTS thread_id VARCHAR(255);

-- Reply tracking (first reply only — our value prop is the first email quality)
ALTER TABLE emails_sent ADD COLUMN IF NOT EXISTS reply_text TEXT;
ALTER TABLE emails_sent ADD COLUMN IF NOT EXISTS reply_received_at TIMESTAMP;
ALTER TABLE emails_sent ADD COLUMN IF NOT EXISTS reply_sentiment VARCHAR(20);  -- positive, negative, neutral

-- Bounce tracking
ALTER TABLE emails_sent ADD COLUMN IF NOT EXISTS bounce_reason TEXT;

-- Flag for "Send Test Emails" feature (permanent — lets user test pipeline with their own addresses)
ALTER TABLE emails_sent ADD COLUMN IF NOT EXISTS is_test BOOLEAN DEFAULT FALSE;

-- Gmail polling state: last time we checked this account's inbox for replies
ALTER TABLE email_accounts ADD COLUMN IF NOT EXISTS last_reply_check_at TIMESTAMP;

-- Index: efficient lookup of sent emails by thread_id for reply matching
CREATE INDEX IF NOT EXISTS idx_emails_sent_thread_id
    ON emails_sent (thread_id)
    WHERE thread_id IS NOT NULL;

-- Index: efficient count of replied/bounced emails per campaign for metrics
CREATE INDEX IF NOT EXISTS idx_emails_sent_status_campaign
    ON emails_sent (campaign_id, status)
    WHERE status IN ('replied', 'bounced');

COMMIT;
