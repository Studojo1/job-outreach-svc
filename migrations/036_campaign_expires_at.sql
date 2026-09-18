-- Migration 036: add expires_at to campaigns for duration-limited plans (e.g. email_50 = 8 days)
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP;
