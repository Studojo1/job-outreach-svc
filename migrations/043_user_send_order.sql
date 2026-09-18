-- 043_user_send_order.sql
-- Let a user pin the order specific leads are emailed in, without giving up
-- score ranking for the ones they never touch.
--
-- Until now the send sequence was decided entirely by lead score, in two
-- separate queries: campaign_service builds the EmailSent rows in score order,
-- and campaign_worker walks them in score order when stamping scheduled_at.
-- A user who wanted "email this company first" had nowhere to say so.
--
-- `send_position` is that place. It is NULL for every existing row, so every
-- campaign already in flight keeps its current ordering exactly. A row with a
-- position sorts ahead of every row without one; rows without a position fall
-- back to score, unchanged. In a 279-lead campaign a user can pin the ten they
-- care about and leave the rest ranked as before.
--
-- Both ordering queries must read this column or the two disagree: rows would
-- be created in one order and scheduled in another, silently. See
-- tests/test_send_order.py, which asserts the two stay in step.

ALTER TABLE emails_sent ADD COLUMN IF NOT EXISTS send_position INTEGER;

-- Partial index: only positioned rows are indexed, which keeps it small even
-- though the column is NULL on the overwhelming majority of rows.
CREATE INDEX IF NOT EXISTS idx_emails_sent_send_position
    ON emails_sent (campaign_id, send_position)
    WHERE send_position IS NOT NULL;

COMMENT ON COLUMN emails_sent.send_position IS
    'User-pinned send order within a campaign (1-based). NULL means unpinned: '
    'the row falls back to lead-score ordering behind all pinned rows.';
