-- Migration 017: Add resume_profile JSONB column to candidates
-- Stores pre-extracted resume intelligence for adaptive quiz engine
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS resume_profile JSONB;
