-- 076_agent_runtime.sql (job-outreach-svc) == drizzle/0037_agent_runtime.sql (frontend)
-- The two files are byte for byte identical. Copy this file; do not retype it.
--
-- Student agent, Phase 1 (staging only). services/agent runs a tool-calling
-- chat for a signed-in student. It reads their profile, matched hiring
-- managers and campaign emails, and writes email DRAFTS that wait for the
-- student. Nothing here is on the send path: no campaign worker phase reads
-- these tables, and Phase 1 never writes emails_sent.
--
--   agent_threads        one conversation (scope 'main' or 'outreach:d_88')
--   agent_runs           one row per user message: status, persisted stream
--                        events, token usage, heartbeat, cross-replica cancel
--   agent_messages       user, assistant and tool rows (the full transcript)
--   agent_drafts         an outreach before it is sent: one email to one
--                        matched lead, with versions and the approval record
--   agent_confirmations  tickets for actions that need the student's click
--   agent_memories       preferences the student asked the agent to keep
--   agent_settings       standing switches, one row per user
--   agent_usage_daily    per user per IST day counters for the caps
--
-- No BEGIN/COMMIT and no CONCURRENTLY: the frontend migrator (scripts/migrate.sh)
-- wraps each file in one transaction. Apply the backend copy the same way:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 --single-transaction -f migrations/076_agent_runtime.sql
-- Idempotent. Apply BEFORE deploying the code that maps it.

CREATE TABLE IF NOT EXISTS agent_threads (
    id              BIGSERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    candidate_id    INTEGER REFERENCES candidates(id) ON DELETE SET NULL,
    scope           VARCHAR(40) NOT NULL DEFAULT 'main',
    title           VARCHAR(80) NOT NULL DEFAULT 'New chat',
    status          VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at      TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at      TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX IF NOT EXISTS ix_agent_threads_user ON agent_threads (user_id, updated_at);
-- One live thread per outreach page. 'main' threads are unlimited (New chat).
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_threads_scope
    ON agent_threads (user_id, scope) WHERE status = 'active' AND scope <> 'main';

CREATE TABLE IF NOT EXISTS agent_runs (
    id                  BIGSERIAL PRIMARY KEY,
    thread_id           BIGINT NOT NULL REFERENCES agent_threads(id) ON DELETE CASCADE,
    user_id             TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    status              VARCHAR(20) NOT NULL DEFAULT 'running',
    events              JSONB NOT NULL DEFAULT '[]'::jsonb,
    model               VARCHAR(60),
    prompt_version      VARCHAR(20),
    confirmation_id     VARCHAR(40),
    steps               INTEGER NOT NULL DEFAULT 0,
    tool_calls          INTEGER NOT NULL DEFAULT 0,
    drafts_created      INTEGER NOT NULL DEFAULT 0,
    tokens_in           INTEGER NOT NULL DEFAULT 0,
    tokens_out          INTEGER NOT NULL DEFAULT 0,
    error               TEXT,
    error_code          VARCHAR(30),
    cancel_requested_at TIMESTAMP,
    heartbeat_at        TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    started_at          TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    finished_at         TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_agent_runs_user_started ON agent_runs (user_id, started_at);
CREATE INDEX IF NOT EXISTS ix_agent_runs_thread ON agent_runs (thread_id, id);
CREATE INDEX IF NOT EXISTS ix_agent_runs_running ON agent_runs (status) WHERE status = 'running';

CREATE TABLE IF NOT EXISTS agent_messages (
    id              BIGSERIAL PRIMARY KEY,
    thread_id       BIGINT NOT NULL REFERENCES agent_threads(id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    run_id          BIGINT REFERENCES agent_runs(id) ON DELETE SET NULL,
    role            VARCHAR(12) NOT NULL,
    content         TEXT NOT NULL DEFAULT '',
    tool_calls      JSONB,
    tool_call_id    VARCHAR(80),
    tool_name       VARCHAR(60),
    meta            JSONB NOT NULL DEFAULT '{}'::jsonb,
    client_turn_id  VARCHAR(64),
    created_at      TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX IF NOT EXISTS ix_agent_messages_thread ON agent_messages (thread_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_messages_turn
    ON agent_messages (thread_id, client_turn_id) WHERE client_turn_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS agent_drafts (
    id                BIGSERIAL PRIMARY KEY,
    user_id           TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    candidate_id      INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    lead_id           INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    thread_id         BIGINT REFERENCES agent_threads(id) ON DELETE SET NULL,
    run_id            BIGINT REFERENCES agent_runs(id) ON DELETE SET NULL,
    kind              VARCHAR(20) NOT NULL DEFAULT 'first_touch',
    status            VARCHAR(20) NOT NULL DEFAULT 'drafting',
    style             VARCHAR(30),
    instructions      VARCHAR(400),
    subject           TEXT NOT NULL DEFAULT '',
    body              TEXT NOT NULL DEFAULT '',
    original_subject  TEXT,
    original_body     TEXT,
    version           INTEGER NOT NULL DEFAULT 0,
    versions          JSONB NOT NULL DEFAULT '[]'::jsonb,
    edited_by         VARCHAR(10) NOT NULL DEFAULT 'agent',
    facts_used        JSONB NOT NULL DEFAULT '[]'::jsonb,
    flags             JSONB NOT NULL DEFAULT '[]'::jsonb,
    failure_reason    VARCHAR(200),
    skip_reason       VARCHAR(200),
    approved_at       TIMESTAMP,
    approved_version  INTEGER,
    approved_hash     VARCHAR(64),
    skipped_at        TIMESTAMP,
    discarded_at      TIMESTAMP,
    discard_reason    VARCHAR(200),
    email_sent_id     INTEGER REFERENCES emails_sent(id) ON DELETE SET NULL,
    heartbeat_at      TIMESTAMP,
    created_at        TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at        TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX IF NOT EXISTS ix_agent_drafts_user_status ON agent_drafts (user_id, status, updated_at);
CREATE INDEX IF NOT EXISTS ix_agent_drafts_lead ON agent_drafts (lead_id);
-- One live row per lead and kind. A redraft supersedes the old row first.
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_drafts_live
    ON agent_drafts (candidate_id, lead_id, kind)
    WHERE status IN ('drafting', 'needs_review', 'approved', 'skipped');

CREATE TABLE IF NOT EXISTS agent_confirmations (
    id          VARCHAR(40) PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    thread_id   BIGINT NOT NULL REFERENCES agent_threads(id) ON DELETE CASCADE,
    run_id      BIGINT REFERENCES agent_runs(id) ON DELETE SET NULL,
    tool        VARCHAR(60) NOT NULL,
    args        JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary     VARCHAR(400) NOT NULL,
    status      VARCHAR(20) NOT NULL DEFAULT 'pending',
    result      JSONB,
    expires_at  TIMESTAMP NOT NULL,
    decided_at  TIMESTAMP,
    created_at  TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX IF NOT EXISTS ix_agent_confirmations_user ON agent_confirmations (user_id, created_at);

CREATE TABLE IF NOT EXISTS agent_memories (
    id             BIGSERIAL PRIMARY KEY,
    user_id        TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    text           VARCHAR(400) NOT NULL,
    scope          VARCHAR(20) NOT NULL DEFAULT 'everywhere',
    company        VARCHAR(200),
    use_in_emails  BOOLEAN NOT NULL DEFAULT false,
    source         VARCHAR(20) NOT NULL DEFAULT 'chat',
    created_at     TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    last_used_at   TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_agent_memories_user ON agent_memories (user_id, created_at);

CREATE TABLE IF NOT EXISTS agent_settings (
    user_id             TEXT PRIMARY KEY REFERENCES "user"(id) ON DELETE CASCADE,
    review_before_send  BOOLEAN NOT NULL DEFAULT true,
    followups           BOOLEAN NOT NULL DEFAULT true,
    never_contact       JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at          TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE IF NOT EXISTS agent_usage_daily (
    user_id    TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    day        DATE NOT NULL,
    messages   INTEGER NOT NULL DEFAULT 0,
    drafts     INTEGER NOT NULL DEFAULT 0,
    revisions  INTEGER NOT NULL DEFAULT 0,
    tokens     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
