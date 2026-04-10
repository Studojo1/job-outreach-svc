-- 023_linkedin_outreach.sql
-- LinkedIn outreach: token storage + async search jobs + leads

CREATE TABLE linkedin_tokens (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    li_at_enc TEXT NOT NULL,        -- base64(AES-256-GCM ciphertext)
    jsessionid_enc TEXT NOT NULL,   -- base64(AES-256-GCM ciphertext)
    nonce TEXT NOT NULL,            -- base64 GCM nonce (12 bytes)
    linkedin_name TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT linkedin_tokens_user_id_unique UNIQUE (user_id)
);

CREATE TABLE linkedin_search_jobs (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    target_role TEXT NOT NULL,
    target_companies TEXT[] DEFAULT '{}',
    location TEXT,
    status TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | failed
    result_count INTEGER DEFAULT 0,
    error TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE linkedin_outreach_leads (
    id SERIAL PRIMARY KEY,
    search_job_id INTEGER NOT NULL REFERENCES linkedin_search_jobs(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    headline TEXT,
    company TEXT,
    profile_url TEXT NOT NULL,
    profile_image_url TEXT,
    suggested_message TEXT,
    message_copied_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_linkedin_search_jobs_user ON linkedin_search_jobs(user_id);
CREATE INDEX idx_linkedin_outreach_leads_job ON linkedin_outreach_leads(search_job_id);
CREATE INDEX idx_linkedin_outreach_leads_user ON linkedin_outreach_leads(user_id);
