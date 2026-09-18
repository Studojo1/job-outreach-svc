-- Migration 019: Add psychometric_profile JSONB column to candidates
-- Stores 4-dimension scoring (analytical/creative/execution/social),
-- detected traits, confidence score, and recommended roles.

ALTER TABLE candidates ADD COLUMN IF NOT EXISTS psychometric_profile JSONB;
