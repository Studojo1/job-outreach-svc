-- 043: Mesa multi-source hardening
-- (a) per-run source telemetry so a silently-broken source is visible
--     (zero-yield streaks) instead of quietly starving a search;
-- (b) cross-source corroboration on jobs: when a second source returns the
--     same company+role, we boost the existing row rather than duplicate it.

CREATE TABLE IF NOT EXISTS mesa_source_runs (
    id          SERIAL PRIMARY KEY,
    search_id   INTEGER NOT NULL REFERENCES mesa_searches(id) ON DELETE CASCADE,
    source      VARCHAR(20) NOT NULL,
    scraped     INTEGER NOT NULL DEFAULT 0,   -- raw rows the source returned
    kept        INTEGER NOT NULL DEFAULT 0,   -- rows surviving relevance/freshness gates
    new_rows    INTEGER NOT NULL DEFAULT 0,   -- rows actually inserted
    error       TEXT,                          -- last error message, NULL on clean run
    ran_at      TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mesa_source_runs_search
    ON mesa_source_runs (search_id, source, ran_at DESC);

ALTER TABLE mesa_jobs
    ADD COLUMN IF NOT EXISTS corroborating_sources TEXT NOT NULL DEFAULT '';
