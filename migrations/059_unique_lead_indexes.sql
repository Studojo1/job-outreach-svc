-- 059_unique_lead_indexes.sql
--
-- B2C open item UC-Q36. Apply after 058, and only once the code that writes
-- leads and lead_scores tolerates conflicts (it used to check-then-insert,
-- which is how the duplicates got in). CONCURRENTLY, so run outside a
-- transaction; it does not block reads or writes.

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_leads_candidate_apollo
    ON leads (candidate_id, apollo_id) WHERE apollo_id IS NOT NULL;

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_lead_scores_lead
    ON lead_scores (lead_id);
