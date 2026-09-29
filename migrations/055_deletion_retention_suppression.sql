-- 055_deletion_retention_suppression.sql
-- Privacy Policy v2.0 §5 and §15, Terms §6.
--
-- 1. suppressed_emails becomes the one list of addresses Studojo never
--    contacts again, whatever the reason: a bounce, a removal request, an
--    opt-out reply, or an admin adding it. Lookups use email_hash (sha256 of
--    the lowercased, trimmed address), so an entry can keep only the hash:
--    §5 promises that after deleting someone's details "we keep only a
--    scrambled (hashed) copy of your address". email becomes nullable and the
--    primary key moves to a new id column.
--    A trigger fills email_hash for writers that only know the old columns
--    (the pre-055 code, anything hand-written), so the table stays usable
--    from both sides while this deploys.
-- 2. removal_requests: third parties who emailed admin@studojo.com. The
--    address is suppressed as soon as a request is logged; deleting their
--    details is due within 30 days (deadline).
-- 3. deleted_account_sends: when an account is deleted, what it sent is kept
--    for 3 years with no content and no readable address (§15): recipient
--    hash, campaign, time, status, touch number, the payment behind it.
--    Written by services/account_deletion.py, pruned by services/retention.py.
--
-- Idempotent: safe to run twice.

BEGIN;

-- ── 1. suppressed_emails ──────────────────────────────────────────────────
ALTER TABLE suppressed_emails ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE suppressed_emails ADD COLUMN IF NOT EXISTS email_hash TEXT;
ALTER TABLE suppressed_emails ADD COLUMN IF NOT EXISTS source TEXT;

UPDATE suppressed_emails SET email = lower(trim(email))
WHERE email IS NOT NULL AND email <> lower(trim(email))
  AND NOT EXISTS (SELECT 1 FROM suppressed_emails s2 WHERE s2.email = lower(trim(suppressed_emails.email)));

UPDATE suppressed_emails
SET email_hash = encode(sha256(convert_to(lower(trim(email)), 'UTF8')), 'hex')
WHERE email_hash IS NULL AND email IS NOT NULL;

-- Two spellings of one address would now share a hash: keep the oldest.
DELETE FROM suppressed_emails a
USING suppressed_emails b
WHERE a.email_hash = b.email_hash AND a.id > b.id;

UPDATE suppressed_emails
SET source = CASE WHEN reason ILIKE 'bounce%' THEN 'bounce' ELSE 'manual' END
WHERE source IS NULL;

ALTER TABLE suppressed_emails ALTER COLUMN email_hash SET NOT NULL;
ALTER TABLE suppressed_emails ALTER COLUMN source SET DEFAULT 'manual';
ALTER TABLE suppressed_emails ALTER COLUMN source SET NOT NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
        WHERE i.indrelid = 'suppressed_emails'::regclass AND i.indisprimary AND a.attname = 'email'
    ) THEN
        ALTER TABLE suppressed_emails DROP CONSTRAINT suppressed_emails_pkey;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_index WHERE indrelid = 'suppressed_emails'::regclass AND indisprimary
    ) THEN
        ALTER TABLE suppressed_emails ADD CONSTRAINT suppressed_emails_pkey PRIMARY KEY (id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'suppressed_emails_source_check'
    ) THEN
        ALTER TABLE suppressed_emails ADD CONSTRAINT suppressed_emails_source_check
            CHECK (source IN ('bounce', 'removal_request', 'reply', 'manual'));
    END IF;
END $$;

ALTER TABLE suppressed_emails ALTER COLUMN email DROP NOT NULL;

-- Full (not partial) unique index on email so the pre-055 code's
-- ON CONFLICT (email) keeps working; NULLs never collide.
CREATE UNIQUE INDEX IF NOT EXISTS suppressed_emails_email_key ON suppressed_emails (email);
CREATE UNIQUE INDEX IF NOT EXISTS suppressed_emails_email_hash_key ON suppressed_emails (email_hash);

CREATE OR REPLACE FUNCTION suppressed_emails_fill_hash() RETURNS trigger AS $$
BEGIN
    IF NEW.email IS NOT NULL THEN
        NEW.email := lower(trim(NEW.email));
        IF NEW.email_hash IS NULL THEN
            NEW.email_hash := encode(sha256(convert_to(NEW.email, 'UTF8')), 'hex');
        END IF;
    END IF;
    -- The pre-055 worker writes bounces with reason 'bounce: ...' and no source.
    IF TG_OP = 'INSERT' AND NEW.source = 'manual' AND NEW.reason ILIKE 'bounce%' THEN
        NEW.source := 'bounce';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS suppressed_emails_fill_hash ON suppressed_emails;
CREATE TRIGGER suppressed_emails_fill_hash
    BEFORE INSERT OR UPDATE ON suppressed_emails
    FOR EACH ROW EXECUTE FUNCTION suppressed_emails_fill_hash();

-- ── 2. removal_requests ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS removal_requests (
    id          BIGSERIAL PRIMARY KEY,
    email       TEXT,                       -- NULL once their details are deleted
    email_hash  TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    deadline    TIMESTAMPTZ NOT NULL,       -- received_at + 30 days (§5)
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    done_at     TIMESTAMPTZ,
    done_by     TEXT,
    result      JSONB,                      -- rows deleted, per table
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS removal_requests_status_deadline_idx ON removal_requests (status, deadline);
CREATE INDEX IF NOT EXISTS removal_requests_email_hash_idx ON removal_requests (email_hash);

-- ── 3. deleted_account_sends ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS deleted_account_sends (
    id               BIGSERIAL PRIMARY KEY,
    channel          TEXT NOT NULL DEFAULT 'email',  -- 'email' | 'linkedin'
    recipient_hash   TEXT NOT NULL,                  -- sha256(lower(trim(address or profile url)))
    campaign_id      INTEGER,
    sent_at          TIMESTAMP,
    status           TEXT,
    followup_number  INTEGER,
    payment_order_id INTEGER,
    deleted_at       TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX IF NOT EXISTS deleted_account_sends_deleted_at_idx ON deleted_account_sends (deleted_at);

COMMIT;
