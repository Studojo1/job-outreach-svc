-- LinkedIn automation: campaigns + connection requests

CREATE TABLE IF NOT EXISTS linkedin_campaigns (
    id                    SERIAL PRIMARY KEY,
    user_id               TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    name                  TEXT NOT NULL,
    status                VARCHAR(20) NOT NULL DEFAULT 'draft',
    target_role           TEXT NOT NULL,
    target_industries     JSONB NOT NULL DEFAULT '[]',
    target_locations      JSONB NOT NULL DEFAULT '[]',
    target_company_sizes  JSONB NOT NULL DEFAULT '[]',
    target_keywords       TEXT,
    connection_note       TEXT,
    followup_message      TEXT,
    daily_limit           INTEGER NOT NULL DEFAULT 20,
    total_leads           INTEGER NOT NULL DEFAULT 0,
    total_sent            INTEGER NOT NULL DEFAULT 0,
    total_accepted        INTEGER NOT NULL DEFAULT 0,
    total_followups_sent  INTEGER NOT NULL DEFAULT 0,
    total_replied         INTEGER NOT NULL DEFAULT 0,
    launched_at           TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS linkedin_connection_requests (
    id                  SERIAL PRIMARY KEY,
    campaign_id         INTEGER NOT NULL REFERENCES linkedin_campaigns(id) ON DELETE CASCADE,
    user_id             TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    headline            TEXT,
    company             TEXT,
    location            TEXT,
    profile_url         TEXT NOT NULL,
    profile_urn         TEXT,
    profile_image_url   TEXT,
    connection_note     TEXT,
    followup_message    TEXT,
    status              VARCHAR(30) NOT NULL DEFAULT 'pending',
    sent_at             TIMESTAMPTZ,
    accepted_at         TIMESTAMPTZ,
    followup_sent_at    TIMESTAMPTZ,
    reply_text          TEXT,
    reply_received_at   TIMESTAMPTZ,
    reply_sentiment     VARCHAR(10),
    error               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_li_campaigns_user ON linkedin_campaigns(user_id);
CREATE INDEX IF NOT EXISTS idx_li_requests_campaign ON linkedin_connection_requests(campaign_id);
CREATE INDEX IF NOT EXISTS idx_li_requests_status ON linkedin_connection_requests(status);
