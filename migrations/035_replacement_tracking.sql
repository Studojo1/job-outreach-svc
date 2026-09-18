-- Migration 035: Auto-replenishment tracking
--
-- When an email bounces or its lead exhausts enrichment retries due to
-- "Apollo could not find this person", the campaign worker promotes the
-- next-best unenriched lead from the user's pool. The new emails_sent row
-- records which original row triggered the replacement.
--
-- We trace lineage (replacement_for_id) and the reason so the dashboard can
-- surface the activity, and the trigger code can guard against replacing a
-- replacement (avoids infinite chains on dead lead pools).

ALTER TABLE emails_sent
    ADD COLUMN IF NOT EXISTS replacement_for_id INTEGER REFERENCES emails_sent(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS replacement_reason VARCHAR(40);
-- replacement_reason values: 'bounce' | 'enrichment_exhausted'

-- Cheap lookup: "which replacements were spawned by email X?"
CREATE INDEX IF NOT EXISTS idx_emails_sent_replacement_for
    ON emails_sent(replacement_for_id)
    WHERE replacement_for_id IS NOT NULL;

-- Cheap count: "how many replacements has campaign C used so far?"
-- Critical for enforcing the 25% per-campaign cap.
CREATE INDEX IF NOT EXISTS idx_emails_sent_campaign_replacement
    ON emails_sent(campaign_id)
    WHERE replacement_for_id IS NOT NULL;
