-- Add connection_mode column to linkedin_tokens to distinguish proxy-bound
-- sessions (email/password login) from extension-driven sessions (browser
-- cookies bound to user's home IP — must be sent via extension, not proxy).

ALTER TABLE linkedin_tokens
  ADD COLUMN IF NOT EXISTS connection_mode TEXT NOT NULL DEFAULT 'proxy';
