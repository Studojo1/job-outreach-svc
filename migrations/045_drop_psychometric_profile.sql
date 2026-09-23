-- 045_drop_psychometric_profile.sql
-- Remove the psychometric_profile column, which no code has written since May 2026.
--
-- It had zero writers anywhere in the codebase and three readers that all
-- resolved to nothing: GET /profile returned it as null, the admin panel echoed
-- an empty dict, and the quiz stream sent a hardcoded null. 1,831 of 6,845
-- candidate rows still held data from when the feature was live.
--
-- Those 1,831 rows were exported before this ran. The export is NOT in the
-- repo (it contains per-user profile text) — it was written to
-- psychometric_profile_backup_2026-09-23.json on the operator's machine.
-- Dropping a populated column cannot be undone from inside the database, so
-- that file is the only copy.
--
-- ORDER MATTERS. The application code that referenced this column must be
-- deployed BEFORE or WITH this migration, not after: a pod still running the
-- old code will raise UndefinedColumn on GET /candidate/{id}/profile as soon as
-- the column disappears. The matching code change is in the same commit.

ALTER TABLE candidates DROP COLUMN IF EXISTS psychometric_profile;
