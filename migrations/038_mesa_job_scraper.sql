-- Mesa: per-client saved LinkedIn job searches + cookie-free scraped results.
CREATE TABLE IF NOT EXISTS mesa_searches (
    id                BIGSERIAL PRIMARY KEY,
    user_id           TEXT NOT NULL,
    name              TEXT NOT NULL,
    keywords          TEXT NOT NULL,
    location          TEXT DEFAULT '',
    date_posted       TEXT DEFAULT '24h',     -- 24h | week | month | any
    workplace_types   TEXT[] DEFAULT '{}',    -- on-site | remote | hybrid
    experience_levels TEXT[] DEFAULT '{}',    -- internship..executive
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_mesa_searches_user ON mesa_searches(user_id);

CREATE TABLE IF NOT EXISTS mesa_jobs (
    id              BIGSERIAL PRIMARY KEY,
    search_id       BIGINT NOT NULL REFERENCES mesa_searches(id) ON DELETE CASCADE,
    linkedin_job_id TEXT NOT NULL,
    title           TEXT,
    company         TEXT,
    location        TEXT,
    posted_date     TEXT,
    url             TEXT,
    scraped_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (search_id, linkedin_job_id)
);
CREATE INDEX IF NOT EXISTS idx_mesa_jobs_search ON mesa_jobs(search_id, scraped_at DESC);
