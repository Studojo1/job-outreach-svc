-- 041: Bob staged-pipeline state.
-- Raw harvest items and the opportunity lifecycle live in the DB, not in the
-- model's context: every opportunity carries an explicit status plus a reject
-- stage/reason, so work can never silently disappear, and interrupted runs
-- resume from persisted state. Replayed idempotently by ensure_schema().

CREATE TABLE IF NOT EXISTS bob_raw_items (
    id           BIGSERIAL PRIMARY KEY,
    chat_id      BIGINT NOT NULL,
    run_id       BIGINT NOT NULL,
    source       TEXT NOT NULL,
    query        TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL,
    url_norm     TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    markdown     TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'harvested',
    note         TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_bob_raw_items_chat_url ON bob_raw_items (chat_id, url_norm);
CREATE INDEX IF NOT EXISTS idx_bob_raw_items_run_status ON bob_raw_items (run_id, status);

CREATE TABLE IF NOT EXISTS bob_opportunities (
    id                  BIGSERIAL PRIMARY KEY,
    chat_id             BIGINT NOT NULL,
    run_id              BIGINT NOT NULL,
    raw_item_id         BIGINT,
    company             TEXT NOT NULL,
    company_norm        TEXT NOT NULL,
    role                TEXT NOT NULL DEFAULT '',
    location            TEXT NOT NULL DEFAULT '',
    stipend             TEXT NOT NULL DEFAULT '',
    posted              TEXT NOT NULL DEFAULT '',
    website             TEXT NOT NULL DEFAULT '',
    evidence_url        TEXT NOT NULL DEFAULT '',
    source              TEXT NOT NULL DEFAULT '',
    evidence_quote      TEXT NOT NULL DEFAULT '',
    what_they_do        TEXT NOT NULL DEFAULT '',
    apply_email         TEXT NOT NULL DEFAULT '',
    apply_person        TEXT NOT NULL DEFAULT '',
    apply_url           TEXT NOT NULL DEFAULT '',
    author_name         TEXT NOT NULL DEFAULT '',
    author_headline     TEXT NOT NULL DEFAULT '',
    author_profile      TEXT NOT NULL DEFAULT '',
    author_affiliation  TEXT NOT NULL DEFAULT 'unknown',
    status              TEXT NOT NULL DEFAULT 'extracted',
    reject_stage        TEXT NOT NULL DEFAULT '',
    reject_reason       TEXT NOT NULL DEFAULT '',
    fit_score           INT,
    fit_reason          TEXT NOT NULL DEFAULT '',
    contact_name        TEXT NOT NULL DEFAULT '',
    contact_title       TEXT NOT NULL DEFAULT '',
    contact_tier        TEXT NOT NULL DEFAULT '',
    contact_source      TEXT NOT NULL DEFAULT '',
    contact_profile_url TEXT NOT NULL DEFAULT '',
    contact_email       TEXT NOT NULL DEFAULT '',
    needs_contact       BOOLEAN NOT NULL DEFAULT false,
    row_id              BIGINT,
    extra               JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bob_opps_run_status ON bob_opportunities (run_id, status);
CREATE INDEX IF NOT EXISTS idx_bob_opps_chat_company ON bob_opportunities (chat_id, company_norm);

ALTER TABLE bob_runs ADD COLUMN IF NOT EXISTS params JSONB NOT NULL DEFAULT '{}'::jsonb;
