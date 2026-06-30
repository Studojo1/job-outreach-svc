-- Mesa multi-source: add `source` to jobs (dedupe per source) and `sources` to searches.
ALTER TABLE mesa_jobs ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'linkedin';
ALTER TABLE mesa_jobs DROP CONSTRAINT IF EXISTS mesa_jobs_search_id_linkedin_job_id_key;
ALTER TABLE mesa_jobs ADD CONSTRAINT mesa_jobs_search_source_extid_key
    UNIQUE (search_id, source, linkedin_job_id);
ALTER TABLE mesa_searches ADD COLUMN IF NOT EXISTS sources TEXT[] DEFAULT '{linkedin,themuse,remotive}';
