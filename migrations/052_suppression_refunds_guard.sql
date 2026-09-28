-- 052_suppression_refunds_guard.sql
-- Post-payment audit wave 5.
--
-- 1. P18: seed suppressed_emails from every address that has already bounced
--    (427 on 28 Sep). The table existed, empty, and nothing used it; the
--    worker now writes it on every bounce and checks it before enriching or
--    sending.
-- 2. P47: pending follow-ups left behind in legacy duplicate groups whose
--    sibling already went out are cancelled (16 on 28 Sep).
-- 3. P05: payment_orders records refunds (status 'refunded', amount, time,
--    provider refund id). Written by services/refunds.py.
-- 4. P26/P30: a campaign cannot gain more paid first-touch rows than it
--    reserved credits for, whatever inserts them. Campaign 124 had 864 rows
--    appended a day after creation, by a script, against 200 credits.
--    Replacement, test and follow-up rows are exempt; campaigns created before
--    the ledger (credits_reserved NULL) are unchecked.
--
-- Apply BEFORE deploying the code that maps the payment_orders columns.

BEGIN;

INSERT INTO suppressed_emails (email, reason)
SELECT DISTINCT lower(trim(to_email)), 'bounce (backfilled by migration 052)'
FROM emails_sent
WHERE status = 'bounced' AND to_email IS NOT NULL AND trim(to_email) <> ''
ON CONFLICT (email) DO NOTHING;

UPDATE emails_sent e
SET status = 'cancelled_duplicate',
    error_message = 'Duplicate follow-up; its sibling was already sent (migration 052)',
    status_changed_at = now() AT TIME ZONE 'utc'
WHERE e.status = 'followup_pending'
  AND e.followup_number > 0
  AND EXISTS (
      SELECT 1 FROM emails_sent s
      WHERE s.parent_email_id = e.parent_email_id
        AND s.followup_number = e.followup_number
        AND s.id <> e.id
        AND s.status IN ('sent', 'replied', 'bounced')
  );

ALTER TABLE payment_orders ADD COLUMN IF NOT EXISTS refunded_cents INTEGER;
ALTER TABLE payment_orders ADD COLUMN IF NOT EXISTS refunded_at TIMESTAMP;
ALTER TABLE payment_orders ADD COLUMN IF NOT EXISTS refund_id TEXT;

CREATE OR REPLACE FUNCTION emails_sent_paid_slot_guard() RETURNS trigger AS $$
DECLARE
    reserved INTEGER;
    existing INTEGER;
BEGIN
    IF NEW.followup_number <> 0 OR NEW.replacement_for_id IS NOT NULL OR COALESCE(NEW.is_test, false) THEN
        RETURN NEW;
    END IF;
    SELECT credits_reserved INTO reserved FROM campaigns WHERE id = NEW.campaign_id;
    IF reserved IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT count(*) INTO existing FROM emails_sent
    WHERE campaign_id = NEW.campaign_id AND followup_number = 0
      AND replacement_for_id IS NULL AND NOT COALESCE(is_test, false);
    IF existing >= reserved THEN
        RAISE EXCEPTION 'campaign % already holds % paid first-touch emails, its full reservation', NEW.campaign_id, reserved
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS emails_sent_paid_slot_guard ON emails_sent;
CREATE TRIGGER emails_sent_paid_slot_guard
    BEFORE INSERT ON emails_sent
    FOR EACH ROW EXECUTE FUNCTION emails_sent_paid_slot_guard();

COMMIT;
