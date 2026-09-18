-- Migration 009: Add enrichment tracking columns to leads table
ALTER TABLE leads ADD COLUMN IF NOT EXISTS company_size VARCHAR(50);
ALTER TABLE leads ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT FALSE;

-- apollo_id was UNIQUE but should allow duplicates across candidates
ALTER TABLE leads DROP CONSTRAINT IF EXISTS leads_apollo_id_key;
