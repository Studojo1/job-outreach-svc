-- 041_mesa_post_fields.sql
-- Post-only columns on mesa_jobs for the linkedin_posts source (null for job rows).
-- Additive + nullable => safe on the shared DB; existing rows unaffected.
ALTER TABLE mesa_jobs ADD COLUMN IF NOT EXISTS author     TEXT;
ALTER TABLE mesa_jobs ADD COLUMN IF NOT EXISTS apply_link TEXT;
ALTER TABLE mesa_jobs ADD COLUMN IF NOT EXISTS post_text  TEXT;
