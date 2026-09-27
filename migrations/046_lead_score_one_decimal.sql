-- 046_lead_score_one_decimal.sql
-- Keep the decimal the scorer computes for lead_scores.overall_score.
--
-- lead_scoring_service rounds every score to one decimal place
-- (lead["score"] = round(normalized_score, 1)) so near-identical leads still
-- rank in a stable order. The column was INTEGER, so Postgres assignment-cast
-- and rounded that decimal away, collapsing leads into integer ties that then
-- came back in whatever order the heap returned them.
--
-- NUMERIC(6,1), not (4,1). Scores are 0-100, but 809 rows (one candidate,
-- written 2026-04-27) hold 49192-50000: a manual display order stored in
-- this column. (4,1) tops out at 999.9, and the first attempt on production
-- failed with "numeric field overflow" and rolled back untouched. (6,1) keeps
-- those rows exactly. Existing integer values convert losslessly (72 -> 72.0);
-- nothing needs backfilling.
--
-- ORDER DOES NOT MATTER for this one. The model change ships as
-- Numeric(6, 1, asdecimal=False): against the old INTEGER column Postgres just
-- keeps rounding on write and reads come back as floats, so code can deploy
-- before or after this runs.
--
-- Cost: changing a column type rewrites lead_scores and holds an ACCESS
-- EXCLUSIVE lock for the duration. Run it off-peak.

-- Measured on production (4.1M rows, 1.4 GB): about 10s under the lock.
SET lock_timeout = '5s';
ALTER TABLE lead_scores ALTER COLUMN overall_score TYPE NUMERIC(6,1);
