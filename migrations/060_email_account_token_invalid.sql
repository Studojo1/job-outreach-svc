-- 060_email_account_token_invalid.sql
-- Audit PS-N14 (29 Sep 2026): a mailbox whose Google refresh token is revoked
-- was refreshed twice every 5 minutes forever. The refresh path now stamps
-- token_invalid_at on invalid_grant; the reply check skips such mailboxes and
-- a reconnect clears it.
ALTER TABLE email_accounts ADD COLUMN IF NOT EXISTS token_invalid_at TIMESTAMP;
