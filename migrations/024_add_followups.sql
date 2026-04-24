-- Migration 024: Add follow-up email support
-- Adds columns to emails_sent for tracking multi-touch outreach sequences.
--
-- followup_number: 0 = initial (Touch 1), 1 = first follow-up (Touch 2), 2 = second follow-up (Touch 3)
-- parent_email_id: points to the Touch 1 row; NULL for Touch 1 itself
-- message_id_header: Gmail's RFC 5322 Message-ID header for proper In-Reply-To threading across email clients

ALTER TABLE emails_sent ADD COLUMN IF NOT EXISTS followup_number INTEGER NOT NULL DEFAULT 0;

ALTER TABLE emails_sent ADD COLUMN IF NOT EXISTS parent_email_id INTEGER REFERENCES emails_sent(id) ON DELETE CASCADE;

ALTER TABLE emails_sent ADD COLUMN IF NOT EXISTS message_id_header VARCHAR(500);

CREATE INDEX IF NOT EXISTS idx_emails_sent_parent
  ON emails_sent(parent_email_id)
  WHERE parent_email_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_emails_sent_followup_pending
  ON emails_sent(status, scheduled_at)
  WHERE status = 'followup_pending';
