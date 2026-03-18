-- Add timezone-aware scheduling columns for campaign worker
-- Campaign: user_timezone for business hours enforcement
-- EmailSent: scheduled_at for per-email scheduling

ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS user_timezone VARCHAR(50) DEFAULT 'Asia/Kolkata';
ALTER TABLE emails_sent ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMP;
