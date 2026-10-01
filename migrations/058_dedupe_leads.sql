-- 058_dedupe_leads.sql
--
-- B2C open item UC-Q36. Nothing stopped the same person being stored twice
-- for one candidate: 13,107 duplicate (candidate_id, apollo_id) groups with
-- 15,806 extra rows (production, 29 Sep; none newer than 12 Aug 2026), plus
-- 3,517 extra lead_scores rows scoring the same lead twice.
--
-- Deleting a duplicate lead cascades to lead_scores and emails_sent, and 306
-- sent emails point at duplicates, so those are re-pointed to the kept lead
-- first. Every removed row is copied to a backup table, so this can be undone.
--
-- Keeps the lowest id in each (candidate_id, apollo_id) group, and the newest
-- lead_scores row per lead (the leads API already treats the newest as current).
-- One transaction. 059 adds the unique indexes afterwards.

BEGIN;

-- Never hold production up: give up rather than wait on a lock or run long.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

CREATE TEMP TABLE _lead_dupes ON COMMIT DROP AS
SELECT id AS dup_id, keep_id
FROM (
    SELECT id,
           MIN(id) OVER (PARTITION BY candidate_id, apollo_id) AS keep_id
    FROM leads
    WHERE apollo_id IS NOT NULL
) x
WHERE id <> keep_id;

CREATE TABLE IF NOT EXISTS backup_058_leads AS SELECT * FROM leads WITH NO DATA;
CREATE TABLE IF NOT EXISTS backup_058_lead_scores AS SELECT * FROM lead_scores WITH NO DATA;
CREATE TABLE IF NOT EXISTS backup_058_emails_sent_repoint (email_id INTEGER, old_lead_id INTEGER, new_lead_id INTEGER);

INSERT INTO backup_058_leads SELECT l.* FROM leads l JOIN _lead_dupes d ON d.dup_id = l.id;

-- Sent emails keep their history on the kept lead.
INSERT INTO backup_058_emails_sent_repoint
SELECT e.id, e.lead_id, d.keep_id FROM emails_sent e JOIN _lead_dupes d ON d.dup_id = e.lead_id;
UPDATE emails_sent e SET lead_id = d.keep_id FROM _lead_dupes d WHERE e.lead_id = d.dup_id;

-- A duplicate's score moves to the kept lead only if the kept lead has none.
UPDATE lead_scores s SET lead_id = d.keep_id
FROM _lead_dupes d
WHERE s.lead_id = d.dup_id
  AND NOT EXISTS (SELECT 1 FROM lead_scores k WHERE k.lead_id = d.keep_id);

INSERT INTO backup_058_lead_scores
SELECT s.* FROM lead_scores s JOIN _lead_dupes d ON d.dup_id = s.lead_id;
DELETE FROM lead_scores s USING _lead_dupes d WHERE s.lead_id = d.dup_id;

DELETE FROM leads l USING _lead_dupes d WHERE l.id = d.dup_id;

-- Same lead scored more than once: keep the newest row. EXISTS on a newer
-- row uses lead_scores_lead_id_idx; NOT IN over the whole table did not
-- finish in 10 minutes.
INSERT INTO backup_058_lead_scores
SELECT s.* FROM lead_scores s
WHERE EXISTS (SELECT 1 FROM lead_scores n WHERE n.lead_id = s.lead_id AND n.id > s.id);
DELETE FROM lead_scores s
USING lead_scores n
WHERE n.lead_id = s.lead_id AND n.id > s.id;

COMMIT;
