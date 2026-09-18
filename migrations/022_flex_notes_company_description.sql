-- Add flex_notes to candidates: stores post-payment project/outcome answers
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS flex_notes JSONB;

-- Add company_description to leads: stores Apollo organization.short_description
ALTER TABLE leads ADD COLUMN IF NOT EXISTS company_description TEXT;
