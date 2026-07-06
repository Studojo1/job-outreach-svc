-- 040: Bob (Mesa) — placement intelligence workspace tables.
-- NOTE: services/bob/schema.py runs this same DDL idempotently at startup,
-- because CI deploys do not apply migrations. This file is the canonical record.

CREATE TABLE IF NOT EXISTS bob_chats (
    id          SERIAL PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT 'New chat',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bob_messages (
    id          SERIAL PRIMARY KEY,
    chat_id     INTEGER NOT NULL REFERENCES bob_chats(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,                -- user | assistant
    content     TEXT NOT NULL DEFAULT '',
    meta        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_bob_messages_chat ON bob_messages(chat_id);

CREATE TABLE IF NOT EXISTS bob_runs (
    id            SERIAL PRIMARY KEY,
    chat_id       INTEGER NOT NULL REFERENCES bob_chats(id) ON DELETE CASCADE,
    status        TEXT NOT NULL DEFAULT 'running',   -- running | waiting_user | done | error
    events        JSONB NOT NULL DEFAULT '[]'::jsonb, -- [{ts,type,label,detail,credits}]
    counters      JSONB NOT NULL DEFAULT '{}'::jsonb, -- {searches,scrapes,credits_used,rows_added}
    credits_used  INTEGER NOT NULL DEFAULT 0,
    answer        TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_bob_runs_chat ON bob_runs(chat_id);

CREATE TABLE IF NOT EXISTS bob_tables (
    id          SERIAL PRIMARY KEY,
    chat_id     INTEGER NOT NULL REFERENCES bob_chats(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    columns     JSONB NOT NULL DEFAULT '[]'::jsonb,  -- [{key,label}]
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_bob_tables_chat ON bob_tables(chat_id);

CREATE TABLE IF NOT EXISTS bob_rows (
    id          SERIAL PRIMARY KEY,
    table_id    INTEGER NOT NULL REFERENCES bob_tables(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL DEFAULT 0,
    cells       JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {column_key: value}
    status      TEXT NOT NULL DEFAULT 'new',         -- new | contacted | replied | meeting | dead
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_bob_rows_table ON bob_rows(table_id);

CREATE TABLE IF NOT EXISTS bob_evidence_cache (
    id            SERIAL PRIMARY KEY,
    cache_key     TEXT NOT NULL UNIQUE,   -- sha256 of normalized request
    kind          TEXT NOT NULL,          -- search | scrape
    request       JSONB NOT NULL DEFAULT '{}'::jsonb,
    payload       JSONB NOT NULL,
    credits_used  INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_bob_evidence_expires ON bob_evidence_cache(expires_at);
