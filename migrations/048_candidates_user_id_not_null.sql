-- 048_candidates_user_id_not_null.sql
-- A resume upload must belong to a user.
--
-- Between 2026-05-29 and 2026-06-29, 104 candidate rows were written with
-- user_id NULL (dev/QA fixtures, per the Sept 2026 signup audit Q31). Nothing
-- rejected them, so the loss was silent. No NULL row has been written since
-- July 2026, so every current writer already sets user_id.
--
-- NOT VALID: enforced for every new INSERT/UPDATE from now on, without
-- scanning or touching the 104 historical rows. Adding it takes only a brief
-- lock (no table scan). To enforce on old rows too, delete or re-link them and
-- then run: ALTER TABLE candidates VALIDATE CONSTRAINT candidates_user_id_not_null;
ALTER TABLE candidates
  ADD CONSTRAINT candidates_user_id_not_null CHECK (user_id IS NOT NULL) NOT VALID;
