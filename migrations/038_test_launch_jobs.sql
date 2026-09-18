-- 038_test_launch_jobs.sql
-- Shared store for async test-launch (deliverability test) jobs.
-- Previously held in an in-memory dict, which 404'd across multiple svc replicas
-- (POST/background thread on one pod, status poll on another). Persisting in
-- Postgres makes the job visible to every replica.

CREATE TABLE IF NOT EXISTS test_launch_jobs (
    job_id     TEXT PRIMARY KEY,
    data       JSONB NOT NULL,
    updated_at TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_test_launch_jobs_updated_at ON test_launch_jobs(updated_at);
