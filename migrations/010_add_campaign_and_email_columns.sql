-- Migration 010: Add campaign settings and email message columns
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS daily_limit INTEGER DEFAULT 20;

ALTER TABLE emails_sent ADD COLUMN IF NOT EXISTS subject TEXT;
ALTER TABLE emails_sent ADD COLUMN IF NOT EXISTS body TEXT;
ALTER TABLE emails_sent ADD COLUMN IF NOT EXISTS to_email VARCHAR(255);

-- Update default status for emails_sent to 'queued'
ALTER TABLE emails_sent ALTER COLUMN status SET DEFAULT 'queued';
